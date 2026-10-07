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
from .cli import DEFAULT_STATE_PATH
from .localproxy import LocalProxyServer, Stats, TunnelFactory
from .systemproxy import system_off, system_on
from .upstream import check_exit_ip
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
        if self._stop_evt is not None:
            self._stop_evt.set()

    # -------------------------------------------------------------- internals
    def _emit(self, **ev) -> None:
        self.events.put(ev)

    def log(self, msg: str, level: str = "info") -> None:
        self._emit(type="log", ts=time.strftime("%H:%M:%S"), level=level, line=msg)

    def _on_servers_rotated(self, new_servers):
        """Upstream credentials were refreshed - notify the tunnel layer."""
        self.log("Upstream server list was refreshed (credentials rotated).")
        self._emit(type="servers_rotated", upstream_ips=self.upstream_ips())

    def _thread_main(self, region, bind, socks_port, http_port, set_system) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                self._amain(region, bind, socks_port, http_port, set_system))
        except Exception as e:  # absolute last-resort guard
            self.log(f"Fatal core error: {e}", "err")
            self._emit(type="phase", phase=PHASE_ERROR, detail=str(e))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _amain(self, region, bind, socks_port, http_port, set_system) -> None:
        api = None
        socks_srv = http_srv = None
        set_system_done = False
        try:
            self.log(f"VPeeN core starting - target region: {region or 'optimal'}")
            self._emit(type="phase", phase=PHASE_CONNECTING, detail=region)
            api = VeePNApi(State(self.state_path), insecure_tls=self.insecure)

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
            self.log(f"Acquired {len(servers)} upstream server(s) for '{region}'.", "ok")

            stats = Stats()
            self.stats = stats
            factory = TunnelFactory(api, servers, insecure_tls=self.insecure,
                                    on_refresh=self._on_servers_rotated)
            self.factory = factory
            proxy = LocalProxyServer(factory, stats)

            async def socks_cb(r, w):
                try:
                    await proxy.handle_socks5(r, w)
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    try:
                        w.close()
                    except Exception:
                        pass

            async def http_cb(r, w):
                try:
                    await proxy.handle_http(r, w)
                except (ConnectionError, OSError):
                    try:
                        w.close()
                    except Exception:
                        pass

            socks_srv = await asyncio.start_server(socks_cb, bind, socks_port)
            http_srv = await asyncio.start_server(http_cb, bind, http_port)
            self.log(f"Local listeners up  |  SOCKS5 {bind}:{socks_port}  |  "
                     f"HTTP {bind}:{http_port}", "ok")

            # sanity-check the tunnel before declaring victory
            exit_ip = None
            for s in servers[:2]:
                try:
                    exit_ip = await check_exit_ip(s, insecure_tls=self.insecure)
                    if exit_ip:
                        break
                except Exception as e:
                    self.log(f"Upstream check failed ({e}) - trying next server...", "warn")

            if exit_ip:
                self._emit(type="exit_ip", ip=exit_ip)
                self.log(f"Connected - exit IP: {exit_ip}", "ok")
            else:
                self.log("Exit IP not verified yet - tunnels keep retrying in background.",
                         "warn")

            if set_system:
                try:
                    system_on(api.state, bind, http_port, socks_port)
                    set_system_done = True
                    self._emit(type="system_proxy", on=True)
                    self.log(f"System proxy set -> {bind}:{http_port}", "ok")
                except Exception as e:
                    self.log(f"Could not set system proxy: {e}", "err")

            self._emit(type="phase", phase=PHASE_CONNECTED, region=region, exit_ip=exit_ip)
            self._emit(type="listeners", socks_port=socks_port, http_port=http_port,
                       upstream_ips=self.upstream_ips())

            # live stats feed until stop is requested
            while not self._stop_evt.is_set():
                await asyncio.sleep(1.0)
                self._emit(type="stats", conns=stats.connections, active=stats.active,
                           failed=stats.failed, up=stats.bytes_up, down=stats.bytes_down)
        except OSError as e:
            self.log(f"Network/listen error: {e}", "err")
            self._emit(type="phase", phase=PHASE_ERROR, detail=str(e))
        except Exception as e:
            self.log(f"Core error: {e}", "err")
            self._emit(type="phase", phase=PHASE_ERROR, detail=str(e))
        finally:
            self.factory = None
            for srv in (socks_srv, http_srv):
                try:
                    if srv is not None:
                        srv.close()
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
                     insecure: bool = False) -> threading.Thread:
    """Fetch 'locations' or 'direct_ip' in a throwaway thread; post one event."""

    def _run() -> None:
        try:
            async def _go():
                api = VeePNApi(State(state_path), insecure_tls=insecure)
                if kind == "locations":
                    all_loc, _free = await api.locations()
                    return all_loc
                if kind == "direct_ip":
                    return await api.check_ip_direct()
                raise ValueError(kind)

            data = asyncio.run(_go())
            events_q.put({"type": kind, "data": data})
        except Exception as e:
            events_q.put({"type": kind, "data": None, "error": str(e)})

    t = threading.Thread(target=_run, daemon=True, name=f"vpeen-{kind}")
    t.start()
    return t
