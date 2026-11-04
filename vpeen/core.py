"""
VPeeN core bridge.

Runs the asyncio proxy stack (api -> upstream -> localproxy) inside a
background thread and reports progress to the GUI through a thread-safe
event queue.  The GUI only ever touches:  Core.start() / Core.stop() /
Core.events  - everything else stays inside this thread.
"""
import asyncio
import queue
import threading
import time

from .api import VeePNApi
from .cli import DEFAULT_STATE_PATH, validate_bind_security
from .localproxy import (READ_LIMIT, LocalProxyServer, Stats,
                         TunnelFactory, install_noise_filter)
from .systemproxy import system_off, system_on
from .upstream import check_exit_ip, probe_server_latency
from .utils import State

PHASE_DISCONNECTED = "disconnected"
PHASE_CONNECTING = "connecting"
PHASE_CONNECTED = "connected"
PHASE_ERROR = "error"


class Core:
    """Background proxy runner with an event feed for the GUI."""

    def __init__(self, state_path: str = DEFAULT_STATE_PATH, insecure: bool = False):
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.state_path = state_path
        self.insecure = insecure
        self.phase = PHASE_DISCONNECTED
        self.stats: Stats | None = None
        self.region = None
        self.factory: TunnelFactory | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt: threading.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def upstream_ips(self) -> list[str]:
        """Current upstream proxy IPs (for tunnel anti-loop host routes)."""
        try:
            return self.factory.upstream_ips() if self.factory else []
        except Exception:
            return []

    # ------------------------------------------------------------ public API
    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, region: str | None, bind: str, socks_port: int, http_port: int,
              set_system: bool) -> None:
        if self.is_busy():
            self.log("Core is already running - ignoring start request.", "warn")
            return
        self.region = region
        self.phase = PHASE_CONNECTING
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(region, bind, socks_port, http_port, set_system),
            daemon=True, name="vpeen-core")
        self._thread.start()

    def stop(self) -> None:
        """Request a stop from the GUI thread.

        v4.2.1: the old stop only set a flag the stats loop polled - if the
        thread was still inside the CONNECT phase (API backoff, server fetch,
        startup checks), disconnect appeared dead for up to minutes.  The
        asyncio task is now cancelled directly, so a stop always lands
        within ~1 second and the finally-block still restores everything."""
        if self._stop_evt is not None:
            self._stop_evt.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop already gone - thread is finishing anyway

    # -------------------------------------------------------------- internals
    def _emit(self, **ev) -> None:
        self.events.put(ev)

    def log(self, msg: str, level: str = "info") -> None:
        self._emit(type="log", ts=time.strftime("%H:%M:%S"), level=level, line=msg)

    def _on_servers_rotated(self, new_servers):
        """Upstream credentials were refreshed - notify the tunnel layer."""
        self.log("Upstream server list was refreshed (credentials rotated).")
        self._emit(type="servers_rotated", upstream_ips=self.upstream_ips())

    async def _verify_exit_ip(self) -> None:
        """v4.5.0: background exit-IP verification (bounded, 2 servers).

        Runs AFTER the connected phase is already emitted - a failed probe
        no longer hides a working tunnel behind 'Connecting...', and a
        successful one just fills in the IP card a moment later."""
        factory = self.factory
        if factory is None:
            return
        for s in list(getattr(factory, "servers", []))[:2]:
            if self._stop_evt.is_set() or factory is not self.factory:
                return
            try:
                ip = await asyncio.wait_for(
                    check_exit_ip(s, insecure_tls=self.insecure,
                                  connect_timeout=6), timeout=15)
                if ip:
                    self._emit(type="exit_ip", ip=ip)
                    self.log(f"Exit IP verified: {ip}", "ok")
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"Upstream check failed ({e}) - trying next server...",
                         "warn")
        self.log("Exit IP not verified yet - tunnels keep retrying in background.",
                 "warn")

    def _thread_main(self, region, bind, socks_port, http_port, set_system) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        install_noise_filter()   # silence WinError 10054 peer-reset noise

        async def _wrapped():
            self._task = asyncio.current_task()
            await self._amain(region, bind, socks_port, http_port, set_system)

        try:
            loop.run_until_complete(_wrapped())
        except asyncio.CancelledError:
            pass  # stop() cancelled us - _amain's finally already cleaned up
        except Exception as e:  # absolute last-resort guard
            self.log(f"Fatal core error: {e}", "err")
            self._emit(type="phase", phase=PHASE_ERROR, detail=str(e))
        finally:
            self._loop = None
            self._task = None
            try:
                loop.close()
            except Exception:
                pass

    async def _amain(self, region, bind, socks_port, http_port, set_system) -> None:
        api = None
        socks_srv = http_srv = None
        client_tasks = set()
        verify_task = None
        set_system_done = False
        try:
            self.log(f"VPeeN core starting - target region: {region or 'optimal'}")
            # v4.3.0: defense in depth - the GUI and CLI validate the bind
            # before calling, but the core has the final say.  A non-loopback
            # bind without credentials would be an open LAN relay.
            problem = validate_bind_security(bind, None, None)
            if problem:
                self.log(problem, "err")
                self._emit(type="phase", phase=PHASE_ERROR,
                           detail="unsafe bind address")
                return
            self._emit(type="phase", phase=PHASE_CONNECTING, detail=region)
            api = VeePNApi(State(self.state_path), insecure_tls=self.insecure)
            if self._stop_evt.is_set():      # stop pressed during startup
                return

            if not region:
                try:
                    loc = await api.optimal_location()
                    if loc.get("proxyType") == 0:
                        region = loc.get("region")
                    else:
                        region = "nl"
                except Exception:
                    region = "nl"
                self.log(f"Auto-selected region: {region}")

            servers = await api.server_list(region)
            if self._stop_evt.is_set():      # stop pressed during API calls
                return
            self.log(f"Acquired {len(servers)} upstream server(s) for '{region}'.", "ok")

            stats = Stats()
            self.stats = stats
            factory = TunnelFactory(api, servers, insecure_tls=self.insecure,
                                    on_refresh=self._on_servers_rotated,
                                    log=self.log)
            self.factory = factory
            # v4.5.0 "Velocity": rank servers by measured RTT (fastest-first,
            # sticky) and start warming the TLS pool immediately.
            factory.start_latency_probe()
            proxy = LocalProxyServer(factory, stats)

            # --- explicit client-task registry: every handler task is tracked
            # so we can cancel and REAP it on stop.  Prevents "Task was
            # destroyed but it is pending!" spam and unretrieved exceptions
            # when the user disconnects with live connections.
            # (client_tasks initialised at the top of _amain)

            def _spawn(coro):
                t = asyncio.ensure_future(coro)
                client_tasks.add(t)

                def _done(task, _set=client_tasks):
                    _set.discard(task)
                    if not task.cancelled():
                        exc = task.exception()  # retrieve so asyncio stays quiet
                        if exc is not None:
                            self.log(f"Connection handler error: "
                                     f"{type(exc).__name__}: {exc}", "warn")
                t.add_done_callback(_done)
                return t

            def socks_cb(r, w):
                _spawn(proxy.handle_socks5(r, w))

            def http_cb(r, w):
                _spawn(proxy.handle_http(r, w))

            socks_srv = await asyncio.start_server(socks_cb, bind, socks_port,
                                                    limit=READ_LIMIT)
            http_srv = await asyncio.start_server(http_cb, bind, http_port,
                                                  limit=READ_LIMIT)
            self.log(f"Local listeners up  |  SOCKS5 {bind}:{socks_port}  |  "
                     f"HTTP {bind}:{http_port}", "ok")

            # v4.5.0 "Velocity": the listeners are up - the proxies are LIVE
            # and usable from this exact moment, so the UI flips to
            # "connected" right away instead of waiting for the exit-IP
            # probe (which used to add up to 15 s of dead air while the
            # tunnel was already working).  The exit IP is verified in the
            # background and arrives as its own event when ready.
            verify_task = asyncio.ensure_future(self._verify_exit_ip())

            if set_system:
                try:
                    system_on(api.state, bind, http_port, socks_port)
                    set_system_done = True
                    self._emit(type="system_proxy", on=True)
                    self.log(f"System proxy set -> {bind}:{http_port}", "ok")
                except Exception as e:
                    self.log(f"Could not set system proxy: {e}", "err")

            self._emit(type="phase", phase=PHASE_CONNECTED, region=region)
            self._emit(type="listeners", socks_port=socks_port, http_port=http_port,
                       upstream_ips=self.upstream_ips())

            # live stats feed until stop is requested
            last_failed = 0
            last_fail_log = 0.0
            last_pool_log = 0.0
            while not self._stop_evt.is_set():
                await asyncio.sleep(1.0)
                # v4.6.0: surface the warm-pool size so the speed machinery
                # is visible in the UI ("N warm") and in the logs.
                # v4.7.0: hedges = hedged connects that rescued a stalled
                # handshake - non-zero means tail-latency protection fired.
                warm = 0
                hedges = 0
                if self.factory is not None:
                    try:
                        ps = self.factory.pool_stats()
                        warm, hedges = ps["warm"], ps["hedges"]
                    except Exception:
                        warm, hedges = 0, 0
                self._emit(type="stats", conns=stats.connections, active=stats.active,
                           failed=stats.failed, up=stats.bytes_up, down=stats.bytes_down,
                           warm=warm, hedges=hedges)
                # v4.6.0: periodic warm-pool health line (every 30 s) - hits
                # vs stale tells the user whether 'Turbo' is actually paying
                # off on their route.  v4.7.0: hedges + adaptive TTL joined
                # the line so Nitro's work is visible too.
                now = time.monotonic()
                if self.factory is not None and now - last_pool_log >= 30.0:
                    last_pool_log = now
                    try:
                        ps = self.factory.pool_stats()
                        self.log(f"warm pool: {ps['warm']} live · "
                                 f"{ps['hits']} hits · {ps['stale']} stale · "
                                 f"{ps['hedges']} hedged · ttl {ps['ttl']}s",
                                 "ok")
                    except Exception:
                        pass
                # surface WHY connections fail (throttled to 1 line / 10 s)
                if stats.failed > last_failed:
                    now = time.monotonic()
                    if now - last_fail_log >= 10.0:
                        n = stats.failed - last_failed
                        last_failed = stats.failed
                        last_fail_log = now
                        self.log(f"{n} connection(s) failed"
                                 f"{stats.reasons_summary()}", "warn")
                elif stats.failed < last_failed:
                    last_failed = stats.failed
        except OSError as e:
            self.log(f"Network/listen error: {e}", "err")
            self._emit(type="phase", phase=PHASE_ERROR, detail=str(e))
        except Exception as e:
            self.log(f"Core error: {e}", "err")
            self._emit(type="phase", phase=PHASE_ERROR, detail=str(e))
        finally:
            try:
                # a stop() cancellation must not abort the cleanup itself
                cur = asyncio.current_task()
                if cur is not None and cur.cancelling() > 0:
                    cur.uncancel()
            except Exception:
                pass
            # v4.5.0: stop the background exit-IP probe and drop the warm
            # pool before anything else so nothing re-opens sockets while
            # the rest of the teardown runs.
            if verify_task is not None and not verify_task.done():
                verify_task.cancel()
            if self.factory is not None:
                try:
                    await self.factory.stop()
                except Exception:
                    pass
            self.factory = None
            self.stats = None
            # ---- graceful, warning-free shutdown ----
            # NOTE: on Python 3.12.1+ Server.wait_closed() waits for ALL
            # client handlers, so handlers must be cancelled+reaped BEFORE
            # awaiting wait_closed, otherwise disconnect deadlocks.
            for srv in (socks_srv, http_srv):
                try:
                    if srv is not None:
                        srv.close()
                except Exception:
                    pass
            try:
                for t in list(client_tasks):
                    t.cancel()
                if client_tasks:
                    await asyncio.gather(*client_tasks, return_exceptions=True)
                for srv in (socks_srv, http_srv):
                    if srv is not None:
                        await asyncio.wait_for(srv.wait_closed(), timeout=10)
            except Exception:
                pass
            if set_system_done and api is not None:
                try:
                    system_off(api.state)
                    self._emit(type="system_proxy", on=False)
                    self.log("System proxy restored.", "ok")
                except Exception:
                    pass
            self.phase = PHASE_DISCONNECTED
            self._emit(type="phase", phase=PHASE_DISCONNECTED)
            self.log("Core stopped.")


# ----------------------------------------------------------------- one-shots
def spawn_quick_task(events_q, kind: str, state_path: str = DEFAULT_STATE_PATH,
                     insecure: bool = False, payload: dict | None = None) -> threading.Thread:
    """Fetch 'locations' / 'direct_ip' / 'pings' in a throwaway thread;
    post one event."""

    def _run() -> None:
        try:
            async def _go():
                api = VeePNApi(State(state_path), insecure_tls=insecure)
                if kind == "locations":
                    all_loc, _free = await api.locations()
                    return all_loc
                if kind == "direct_ip":
                    return await api.check_ip_direct()
                if kind == "pings":
                    # v4.2.1: real latency probe so the 'Fastest' sort is
                    # honest.  For each free region: one server_list fetch
                    # (also warms the 12h server cache -> instant region
                    # switching later) + a TCP connect-time probe.
                    # v4.5.0: probe EVERY server of the region (up to 5,
                    # concurrently) and report the best one, so a region is
                    # not misjudged because its first entry happens to be a
                    # slow/blackholed address.  Regions that fail keep no
                    # ping.
                    regions = (payload or {}).get("regions") or []
                    out = {}
                    for region in regions:
                        try:
                            servers = await api.server_list(region)
                            if not servers:
                                continue
                            measured = await probe_server_latency(
                                servers[:5], budget=2.5)
                            if measured:
                                out[region] = min(measured.values())
                        except Exception:
                            continue
                    return out
                raise ValueError(kind)

            data = asyncio.run(_go())
            events_q.put({"type": kind, "data": data})
        except Exception as e:
            events_q.put({"type": kind, "data": None, "error": str(e)})

    t = threading.Thread(target=_run, daemon=True, name=f"vpeen-{kind}")
    t.start()
    return t
