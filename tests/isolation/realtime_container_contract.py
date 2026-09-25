"""Exact machine-consumed contracts for the optional realtime deploy unit."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = b"""#!/usr/bin/env sh
set -eu

case "${CLINIC_PROCESS_PURPOSE:-}" in
  web|release|realtime) ;;
  *) exit 64 ;;
esac
exec "$@"
"""
NGINX = (
    b"# Same origin keeps CSP connect-src 'self'; POST stays WSGI, only GET is ASGI.\n"
    b"""map $request_method $stream_upstream {
    default http://web:8000;
    GET http://realtime:8001;
}
server {
    listen 8080;
    server_name localhost;
    resolver 127.0.0.11 valid=10s; # Docker's internal DNS for the method map.
    # Stream tickets are credentials; never log a query string.
    access_log off;
    location = /rt/stream {
        proxy_pass $stream_upstream;
        proxy_set_header Host $http_host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 620s;
    }
    location / {
        proxy_pass http://web:8000;
        proxy_set_header Host $http_host;
    }
}
"""
)


def assert_realtime_contracts() -> None:
    assert (ROOT / "ops/container/entrypoint.sh").read_bytes() == ENTRYPOINT
    assert (ROOT / "ops/container/realtime-nginx.conf").read_bytes() == NGINX
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    expected = json.loads(
        Path(__file__).with_name("realtime-compose-contract.json").read_text()
    )
    assert set(compose["services"]) == {"db", *expected}
    assert {name: compose["services"][name] for name in expected} == expected
