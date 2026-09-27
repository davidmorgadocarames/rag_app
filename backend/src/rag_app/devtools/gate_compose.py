"""Isolation guard for the gate project (``compose.gate.yml``), run before every ``up``.

Reads the resolved configuration (``docker compose … config --format json``) on stdin, or
a file, and refuses anything that could touch the development stack:

- the project name must be ``secrag-gate``;
- every volume must be a named volume whose Docker name starts with ``secrag_gate_``
  (no bind mounts, no external volumes — the dev volumes are ``rag_ia_pgdata`` /
  ``rag_app_pgdata``);
- every published port must bind ``127.0.0.1`` and must not be a development port
  (5432, 8000, 3000, 11434).

    docker compose -p secrag-gate -f compose.gate.yml config --format json \\
        | python -m rag_app.devtools.gate_compose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT = "secrag-gate"
VOLUME_PREFIX = "secrag_gate_"
DEV_PORTS = frozenset({5432, 8000, 3000, 11434})


def problems(config: dict[str, Any]) -> list[str]:
    """Every isolation violation in a resolved compose configuration."""
    found: list[str] = []
    if config.get("name") != PROJECT:
        found.append(f"project name is {config.get('name')!r}, expected {PROJECT!r}")

    volumes: dict[str, Any] = config.get("volumes") or {}
    for key, spec in volumes.items():
        spec = spec or {}
        name = str(spec.get("name", ""))
        if spec.get("external"):
            found.append(f"volume {key!r} is external")
        if not name.startswith(VOLUME_PREFIX):
            found.append(f"volume {key!r} has Docker name {name!r} (must start {VOLUME_PREFIX})")

    for svc_name, svc in (config.get("services") or {}).items():
        for mount in svc.get("volumes") or []:
            kind = mount.get("type")
            if kind != "volume":
                found.append(f"service {svc_name!r}: {kind} mount {mount.get('source')!r}")
            elif mount.get("source") not in volumes:
                found.append(f"service {svc_name!r}: undeclared volume {mount.get('source')!r}")
        for port in svc.get("ports") or []:
            published = str(port.get("published", ""))
            host_ip = port.get("host_ip", "")
            if host_ip != "127.0.0.1":
                found.append(f"service {svc_name!r}: port {published} binds {host_ip or '0.0.0.0'}")
            if published.isdigit() and int(published) in DEV_PORTS:
                found.append(f"service {svc_name!r}: port {published} is a development port")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the gate project's isolation.")
    parser.add_argument("config", nargs="?", type=Path, help="JSON file (default: stdin)")
    args = parser.parse_args(argv)
    raw = args.config.read_text(encoding="utf-8") if args.config else sys.stdin.read()
    found = problems(json.loads(raw))
    for item in found:
        print(f"  gate project NOT isolated: {item}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
