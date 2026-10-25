#!/usr/bin/env python3
"""CloudBox node agent.

Runs on a machine that hosts LXC containers and exposes a single signed
endpoint the bot can call. Bind it to loopback and put TLS in front of it
(Caddy, nginx, WireGuard) — the HMAC protects integrity, not confidentiality.

    AGENT_SECRET=... AGENT_HOST=127.0.0.1 AGENT_PORT=8099 python agent.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import time

from aiohttp import web

SECRET = (os.getenv("AGENT_SECRET") or "").strip()
HOST = os.getenv("AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("AGENT_PORT", "8099"))
MAX_SKEW = 300  # seconds
CONCURRENCY = asyncio.Semaphore(8)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cloudbox.agent")


def _problem(request: web.Request, body: bytes) -> str | None:
    if not SECRET:
        return "agent has no secret configured"

    issued = request.headers.get("X-CloudBox-Timestamp", "")
    signature = request.headers.get("X-CloudBox-Signature", "")
    if not issued or not signature:
        return "missing signature"

    try:
        moment = int(issued)
    except ValueError:
        return "malformed timestamp"

    if abs(time.time() - moment) > MAX_SKEW:
        return "stale request"

    expected = hmac.new(
        SECRET.encode(), issued.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "bad signature"
    return None


async def run(request: web.Request) -> web.Response:
    body = await request.read()
    problem = _problem(request, body)
    if problem:
        log.warning("rejected request: %s", problem)
        return web.json_response({"error": problem}, status=403)

    try:
        payload = json.loads(body or b"{}")
        argv = [str(part) for part in payload.get("argv", [])]
    except (ValueError, TypeError):
        return web.json_response({"error": "malformed body"}, status=400)

    if not argv or argv[0] != "lxc":
        return web.json_response({"error": "only lxc commands are accepted"}, status=400)

    async with CONCURRENCY:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=900)
        except FileNotFoundError:
            return web.json_response({"error": "lxc is not installed"}, status=500)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return web.json_response({"error": "command timed out"}, status=504)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("execution failed")
            return web.json_response({"error": str(exc)}, status=500)

    return web.json_response(
        {
            "code": proc.returncode or 0,
            "out": out.decode(errors="replace"),
            "err": err.decode(errors="replace"),
        }
    )


async def stats(request: web.Request) -> web.Response:
    body = await request.read()
    problem = _problem(request, body)
    if problem:
        return web.json_response({"error": problem}, status=403)

    usage = shutil.disk_usage("/")
    payload = {
        "disk_total_gb": round(usage.total / 1024**3, 1),
        "disk_used_gb": round(usage.used / 1024**3, 1),
    }
    try:
        import psutil

        memory = psutil.virtual_memory()
        payload.update(
            {
                "cpu_pct": psutil.cpu_percent(interval=0.3),
                "mem_pct": memory.percent,
                "mem_total_gb": round(memory.total / 1024**3, 1),
                "mem_used_gb": round(memory.used / 1024**3, 1),
                "cores": psutil.cpu_count(logical=True),
            }
        )
    except ImportError:
        payload["note"] = "install psutil for richer stats"

    return web.json_response(payload)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def app() -> web.Application:
    application = web.Application(client_max_size=1 << 20)
    application.add_routes(
        [
            web.post("/run", run),
            web.post("/stats", stats),
            web.get("/health", health),
        ]
    )
    return application


if __name__ == "__main__":
    if not SECRET:
        raise SystemExit("AGENT_SECRET must be set")
    log.info("listening on %s:%s", HOST, PORT)
    web.run_app(app(), host=HOST, port=PORT, access_log=None)
