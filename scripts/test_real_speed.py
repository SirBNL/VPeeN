#!/usr/bin/env python3
"""v4.6.0 'Turbo' real-world speed measurement.

Connects to the REAL VeePN free-proxy network (fresh credentials),
then measures, through TunnelFactory exactly as the app uses it:

  A) cold path  - every connection pays TCP + TLS + CONNECT (v4.5-like
     worst case: pool disabled via POOL_SIZE=0)
  B) turbo path - v4.6.0 pool-aware scheduling with the warm pool

For each path it opens N connections sequentially (like a browser
opening sockets for a page load), issues a small HTTPS GET through the
tunnel via Cloudflare's speed endpoint, and reports:
  * time from open() to CONNECT-200  (connection setup)
  * total wall time for all N connections
  * effective KB/s while downloading 1 MiB per connection

Run: python3 scripts/test_real_speed.py [region]
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vpeen.localproxy as localproxy
from vpeen.api import VeePNApi
from vpeen.localproxy import TunnelFactory
from vpeen.utils import State

REGION = sys.argv[1] if len(sys.argv) > 1 else "nl"
N_CONNS = 6                     # like one browser page load
DL_PATH = "/__down?bytes=262144"   # 256 KiB per connection

LOGS = []


def log(msg, lvl="info"):
    LOGS.append(msg)
    print(f"    {msg}")


async def fetch_through(reader, writer, path=DL_PATH, budget=20.0):
    """GET speed.cloudflare.com path through an open tunnel, return bytes."""
    req = (f"GET {path} HTTP/1.1\r\nHost: speed.cloudflare.com\r\n"
           f"User-Agent: vpeen-test\r\nAccept: */*\r\nConnection: close\r\n\r\n")
    writer.write(req.encode())
    await writer.drain()
    total = 0
    while True:
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=budget)
        except asyncio.TimeoutError:
            break
        if not data:
            break
        total += len(data)
    return total


async def run_path(factory, label):
    t_total0 = time.monotonic()
    setups = []
    total_bytes = 0
    for i in range(N_CONNS):
        t0 = time.monotonic()
        r, w = await factory.open("speed.cloudflare.com", 80)
        setups.append(time.monotonic() - t0)
        n = await fetch_through(r, w)
        total_bytes += n
        w.close()
    dt = time.monotonic() - t_total0
    avg_setup = sum(setups) / len(setups)
    kbps = total_bytes / 1024.0 / dt
    print(f"  [{label}] {N_CONNS} conns | avg setup {avg_setup*1000:.0f} ms | "
          f"total {dt:.2f}s | {total_bytes/1024:.0f} KiB | {kbps:.0f} KiB/s")
    return avg_setup, dt, kbps


async def await_probes(factory, timeout=12.0):
    """_probe_servers ends in a periodic re-probe loop, so wait for the
    throughput probe to settle instead of awaiting the task."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if factory.server_throughput or factory._closing:
            return
        await asyncio.sleep(0.1)


async def main():
    print(f"VPeeN v4.6.0 real-world speed test (region={REGION})")
    api = VeePNApi(State("/tmp/vpeen-speedtest-state.json"))
    servers = await api.server_list(REGION)
    print(f"  got {len(servers)} upstream servers for '{REGION}'")

    # ---------------- path A: COLD (no warm pool) ----------------
    localproxy.POOL_SIZE_SAVE = localproxy.POOL_SIZE
    localproxy.POOL_SIZE = 0
    cold_factory = TunnelFactory(api, servers, insecure_tls=False, log=log)
    cold_factory.start_latency_probe()
    await await_probes(cold_factory)
    a_setup, a_dt, a_kbps = await run_path(cold_factory, "COLD (v4.5-like)")
    await cold_factory.stop()

    # ---------------- path B: TURBO (v4.6.0 pool) ----------------
    localproxy.POOL_SIZE = localproxy.POOL_SIZE_SAVE
    turbo_factory = TunnelFactory(api, servers, insecure_tls=False, log=log)
    turbo_factory.start_latency_probe()
    await await_probes(turbo_factory)
    # give the refiller a moment to pre-warm after the probes
    await asyncio.sleep(2.0)
    ps = turbo_factory.pool_stats()
    print(f"  warm pool after pre-warm: {ps}")
    b_setup, b_dt, b_kbps = await run_path(turbo_factory, "TURBO (v4.6.0)")
    await turbo_factory.stop()

    print("\n  ================= SUMMARY ================")
    print(f"  connection setup : {a_setup*1000:7.0f} ms  ->  {b_setup*1000:7.0f} ms   "
          f"({(a_setup/max(b_setup,1e-9)):,.1f}x faster)")
    print(f"  total for {N_CONNS} conns : {a_dt:7.2f} s  ->  {b_dt:7.2f} s   "
          f"({(a_dt/max(b_dt,1e-9)):,.1f}x faster)")
    print(f"  effective KB/s   : {a_kbps:7.0f}     ->  {b_kbps:7.0f}     "
          f"({(b_kbps/max(a_kbps,1e-9)):,.1f}x)")


if __name__ == "__main__":
    asyncio.run(main())
