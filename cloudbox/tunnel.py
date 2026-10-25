"""Reverse SSH tunnels through pinggy.io so users can reach their container."""

from __future__ import annotations

import asyncio
import re

from .nodes import Executor, NodeError

LOG_PATH = "/root/.cloudbox-tunnel.log"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
ADDRESS = re.compile(r"tcp://([A-Za-z0-9.\-]+):(\d+)")

SSHD_BOOTSTRAP = r"""
set -u
mkdir -p /run/sshd
if ! command -v sshd >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq openssh-server >/dev/null 2>&1
fi
service ssh start >/dev/null 2>&1 \
  || service sshd start >/dev/null 2>&1 \
  || systemctl start ssh >/dev/null 2>&1 \
  || /usr/sbin/sshd >/dev/null 2>&1 || true
"""


class TunnelManager:
    def __init__(self, token: str, host: str) -> None:
        # Both values end up inside a guest shell script, so keep them boring.
        self.token = re.sub(r"[^A-Za-z0-9_-]", "", token or "")
        self.host = re.sub(r"[^A-Za-z0-9.\-]", "", host or "") or "free.pinggy.io"

    @property
    def user(self) -> str:
        return f"{self.token}+tcp" if self.token else "qr+tcp"

    async def open(
        self,
        executor: Executor,
        container: str,
        attempts: int = 8,
        delay: float = 3.0,
    ) -> str | None:
        """Start a fresh tunnel and return ``host:port`` once it is assigned."""
        await self._exec(executor, container, SSHD_BOOTSTRAP, timeout=180)

        script = f"""
set +e
pkill -9 -f 'pinggy.io' >/dev/null 2>&1
rm -f {LOG_PATH}
command -v ssh >/dev/null 2>&1 || ( \
  export DEBIAN_FRONTEND=noninteractive; \
  apt-get update -qq >/dev/null 2>&1; \
  apt-get install -y -qq openssh-client >/dev/null 2>&1 )
setsid ssh -p 443 -T \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -R0:localhost:22 {self.user}@{self.host} >> {LOG_PATH} 2>&1 < /dev/null &
"""
        await self._exec(executor, container, script, timeout=60)

        for _ in range(attempts):
            await asyncio.sleep(delay)
            log = await self._read_log(executor, container)
            match = ADDRESS.search(ANSI.sub("", log))
            if match:
                return f"{match.group(1)}:{match.group(2)}"
            lowered = log.lower()
            if "permission denied" in lowered or "connection refused" in lowered:
                return None
        return None

    async def _read_log(self, executor: Executor, container: str) -> str:
        try:
            result = await executor.run(
                ["exec", container, "--", "cat", LOG_PATH], timeout=30
            )
        except NodeError:
            return ""
        return result.out or result.err

    async def _exec(
        self, executor: Executor, container: str, script: str, timeout: float
    ) -> None:
        result = await executor.run(
            ["exec", container, "--", "sh", "-lc", script], timeout=timeout
        )
        if not result.ok:
            # Tunnel setup is best-effort; callers surface a friendly message.
            raise NodeError(result.err or "command failed inside the container")
