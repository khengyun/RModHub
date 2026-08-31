"""Container entrypoint of the RModHub API image (`ENTRYPOINT` in the root Dockerfile).

Two deployment guards that belong to the container rather than to the application, then
`exec` of the command (`CMD`: uvicorn). Stdlib only; runs under /app/.venv/bin/python.

1. Trusted proxies for uvicorn's `--proxy-headers`.
   The per-address quota (app/jobs/quota.py) keys on `request.client.host`, which uvicorn
   takes from `X-Forwarded-For` **when the direct peer is a trusted proxy**; it walks the
   header from the right and stops at the first address that is not trusted. Trusting
   every hop (`--forwarded-allow-ips=*`) would make uvicorn take the *first* entry, i.e.
   whatever the browser wrote, and let anyone pick their own quota key. So the trusted
   set is exactly the container's own network(s) -- the compose network that `web`
   (nginx) and `caddy` sit on -- read from /proc/net/route and /proc/net/ipv6_route
   at start-up, **minus the default gateway**: connections that Docker's userland proxy
   relays to a published port (the host itself, IPv6 clients, Docker Desktop) arrive
   from that address, so it must not be able to vouch for anyone. Peers outside the
   networks (a published port reached with iptables NAT, host networking) are never
   trusted either. `RMODHUB_TRUSTED_PROXIES` / `FORWARDED_ALLOW_IPS` replace the
   detected list, e.g. with the gateway address when a reverse proxy runs on the host.

2. Refuse to start the signal branch with the development HMAC key.
   `client_key = HMAC(RMODHUB_IP_HASH_SECRET, ip)` is what the `jobs` table stores
   instead of the address; with the public default key ("rmodhub-dev") the IPv4 space
   can be hashed in seconds and every stored key reversed. When a database URL is set
   (branch enabled) and the key is missing, empty or the default, the container exits
   with a message instead of serving. `RMODHUB_ALLOW_DEV_SECRET=1` opts out (local
   experiments only).

`python api-entrypoint.py --show` prints the computed trusted-proxy list and exits.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Iterable, Mapping

DEV_IP_HASH_SECRET = "rmodhub-dev"  # app/config.py::DEFAULT_IP_HASH_SECRET
FALLBACK_TRUSTED = "127.0.0.1"  # uvicorn's own default: trust nothing beyond loopback
RTF_UP = 0x1
PROC_ROUTE_V4 = "/proc/net/route"
PROC_ROUTE_V6 = "/proc/net/ipv6_route"


Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _routes_v4(lines: Iterable[str]) -> tuple[list[ipaddress.IPv4Network], list[ipaddress.IPv4Address]]:
    """(attached networks, default gateways) from /proc/net/route (little-endian hex)."""
    nets, gateways = [], []
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[0] == "Iface":
            continue
        iface, dest, gw, flags, _rc, _use, _metric, mask = fields[:8]
        try:
            if iface == "lo" or not int(flags, 16) & RTF_UP:
                continue
            mask_int = int.from_bytes(bytes.fromhex(mask), "little")
            dest_int = int.from_bytes(bytes.fromhex(dest), "little")
            gw_int = int.from_bytes(bytes.fromhex(gw), "little")
        except ValueError:
            continue
        if mask_int == 0:  # default route: remember who relays for us
            if gw_int:
                gateways.append(ipaddress.IPv4Address(gw_int))
            continue
        nets.append(ipaddress.IPv4Network((dest_int, mask_int.bit_count()), strict=False))
    return nets, gateways


def _routes_v6(lines: Iterable[str]) -> tuple[list[ipaddress.IPv6Network], list[ipaddress.IPv6Address]]:
    """(attached networks, default gateways) from /proc/net/ipv6_route (skips multicast)."""
    nets, gateways = [], []
    for line in lines:
        fields = line.split()
        if len(fields) < 10:
            continue
        dest, prefix, _src, _srcprefix, nexthop, _metric, _rc, _use, flags, iface = fields[:10]
        try:
            if iface == "lo" or not int(flags, 16) & RTF_UP:
                continue
            prefixlen = int(prefix, 16)
            if prefixlen == 0:
                gw = ipaddress.IPv6Address(int(nexthop, 16))
                if not gw.is_unspecified:
                    gateways.append(gw)
                continue
            net = ipaddress.IPv6Network((int(dest, 16), prefixlen), strict=False)
        except ValueError:
            continue
        if not net.is_multicast:
            nets.append(net)
    return nets, gateways


def _without(nets: Iterable[Network], addresses: Iterable[Address]) -> list[Network]:
    """`nets` as the minimal CIDR set that leaves the given addresses out."""
    pieces: list[Network] = list(nets)
    for addr in addresses:
        host = ipaddress.ip_network(addr)
        kept: list[Network] = []
        for net in pieces:
            if net.version != host.version or host == net:
                if host != net:
                    kept.append(net)
                continue
            kept.extend(net.address_exclude(host) if host.subnet_of(net) else [net])
        pieces = kept
    return pieces


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return []


def detect_trusted_proxies() -> str:
    """Comma-separated attached networks minus the default gateway(s), or the loopback fallback."""
    nets4, gws4 = _routes_v4(_read_lines(PROC_ROUTE_V4))
    nets6, gws6 = _routes_v6(_read_lines(PROC_ROUTE_V6))
    nets = _without([*nets4, *nets6], [*gws4, *gws6])
    unique = sorted(set(nets), key=lambda n: (n.version, n.network_address, n.prefixlen))
    return ",".join(str(n) for n in unique) if unique else FALLBACK_TRUSTED


def trusted_proxies(env: Mapping[str, str]) -> tuple[str, str]:
    """(value, origin) -- explicit `RMODHUB_TRUSTED_PROXIES` / `FORWARDED_ALLOW_IPS`, else detected."""
    for name in ("RMODHUB_TRUSTED_PROXIES", "FORWARDED_ALLOW_IPS"):
        value = env.get(name, "").strip()
        if value:
            return value, name
    return detect_trusted_proxies(), "detected"


def signal_branch_enabled(env: Mapping[str, str]) -> bool:
    return bool(env.get("RMODHUB_DATABASE_URL", "").strip() or env.get("DATABASE_URL", "").strip())


def ip_hash_secret_error(env: Mapping[str, str]) -> str | None:
    """Message when the signal branch would run with an unusable HMAC key, else None."""
    if not signal_branch_enabled(env):
        return None
    secret = env.get("RMODHUB_IP_HASH_SECRET", "")
    if secret and secret != DEV_IP_HASH_SECRET:
        return None
    if env.get("RMODHUB_ALLOW_DEV_SECRET", "").strip().lower() in {"1", "true", "yes"}:
        return None
    what = "not set" if not secret else f"the development default ({DEV_IP_HASH_SECRET!r})"
    return (
        f"RMODHUB_IP_HASH_SECRET is {what} while the signal branch is enabled (DATABASE_URL "
        "is set). It is the HMAC key that replaces client addresses in the jobs table; with "
        "a known key every stored key can be reversed. Set a random value, e.g. "
        "RMODHUB_IP_HASH_SECRET=$(openssl rand -hex 32) in .env, or "
        "RMODHUB_ALLOW_DEV_SECRET=1 for a local experiment."
    )


def main(argv: list[str]) -> int:
    env = os.environ
    value, origin = trusted_proxies(env)
    if argv[:1] == ["--show"]:
        print(value)
        return 0
    if origin == "detected" and value == FALLBACK_TRUSTED:
        print(
            "api-entrypoint: no attached network found; X-Forwarded-* is only trusted from "
            "127.0.0.1 (set RMODHUB_TRUSTED_PROXIES to the reverse proxy's address/CIDR)",
            file=sys.stderr,
        )
    print(f"api-entrypoint: trusted proxies for X-Forwarded-* = {value} ({origin})", file=sys.stderr)
    os.environ["FORWARDED_ALLOW_IPS"] = value

    error = ip_hash_secret_error(env)
    if error:
        print(f"api-entrypoint: refusing to start: {error}", file=sys.stderr)
        return 3
    if signal_branch_enabled(env) and env.get("RMODHUB_IP_HASH_SECRET", "") == DEV_IP_HASH_SECRET:
        print(
            "api-entrypoint: WARNING: running with the development RMODHUB_IP_HASH_SECRET "
            "(RMODHUB_ALLOW_DEV_SECRET is set); stored client keys are reversible",
            file=sys.stderr,
        )

    if not argv:
        print("api-entrypoint: no command given (expected e.g. uvicorn app.main:app ...)", file=sys.stderr)
        return 2
    sys.stderr.flush()
    os.execvp(argv[0], argv)
    return 1  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
