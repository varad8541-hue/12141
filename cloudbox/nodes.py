"""Transport layer: run LXC on the local host or on a remote node agent."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Sequence

import aiohttp

# Node agents only ever receive argv lists. Nothing is passed through a shell,
# so a container name can never turn into a command.


@dataclass(slots=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def text(self) -> str:
        return self.out or self.err


class NodeError(RuntimeError):
    """Raised when a node cannot be reached or a command fails."""


class Executor:
    kind = "local"
    label = "local"

    async def run(self, argv: Sequence[str], timeout: float = 120.0) -> Result:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class LocalExecutor(Executor):
    def __init__(self, binary: str = "lxc") -> None:
        self.binary = binary

    async def run(self, argv: Sequence[str], timeout: float = 120.0) -> Result:
        args = [self.binary, *argv]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise NodeError(f"`{self.binary}` is not installed on this host") from exc

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise NodeError(f"command timed out after {int(timeout)}s") from None

        return Result(
            proc.returncode or 0,
            out.decode(errors="replace").strip(),
            err.decode(errors="replace").strip(),
        )


class RemoteExecutor(Executor):
    kind = "remote"

    def __init__(self, label: str, endpoint: str, secret: str) -> None:
        self.label = label
        self.endpoint = endpoint.rstrip("/")
        self._secret = secret.encode()
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=10)
            )
        return self._session

    def _sign(self, body: bytes) -> tuple[str, str]:
        issued = str(int(time.time()))
        digest = hmac.new(self._secret, issued.encode() + b"." + body, hashlib.sha256)
        return issued, digest.hexdigest()

    async def run(self, argv: Sequence[str], timeout: float = 120.0) -> Result:
        if not self.endpoint:
            raise NodeError(f"node `{self.label}` has no endpoint configured")

        body = json.dumps({"argv": list(argv)}).encode()
        issued, signature = self._sign(body)
        headers = {
            "Content-Type": "application/json",
            "X-CloudBox-Timestamp": issued,
            "X-CloudBox-Signature": signature,
        }

        session = await self._http()
        try:
            async with session.post(
                f"{self.endpoint}/run", data=body, headers=headers, timeout=timeout
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200:
                    detail = payload.get("error") if isinstance(payload, dict) else None
                    raise NodeError(detail or f"node returned HTTP {response.status}")
        except asyncio.TimeoutError:
            raise NodeError(f"node `{self.label}` timed out after {int(timeout)}s") from None
        except aiohttp.ClientError as exc:
            raise NodeError(f"node `{self.label}` unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise NodeError(f"node `{self.label}` sent a malformed reply") from exc

        return Result(
            int(payload.get("code", 0)),
            str(payload.get("out", "")).strip(),
            str(payload.get("err", "")).strip(),
        )

    async def stats(self) -> dict[str, Any]:
        session = await self._http()
        headers: dict[str, str] = {}
        body = b"{}"
        issued, signature = self._sign(body)
        headers["X-CloudBox-Timestamp"] = issued
        headers["X-CloudBox-Signature"] = signature
        async with session.post(f"{self.endpoint}/stats", data=body, headers=headers) as response:
            return await response.json(content_type=None)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class NodeRegistry:
    """Builds and caches an executor per stored node."""

    def __init__(self, store: Any, vault: Any) -> None:
        self.store = store
        self.vault = vault
        self._cache: dict[int, Executor] = {}

    def executor_for(self, node: dict[str, Any]) -> Executor:
        node_id = int(node["id"])
        cached = self._cache.get(node_id)
        if cached is not None:
            return cached

        if node["kind"] == "local":
            executor: Executor = LocalExecutor()
        else:
            executor = RemoteExecutor(
                node["name"], node["endpoint"], self.vault.open(node["secret"])
            )
        self._cache[node_id] = executor
        return executor

    def invalidate(self, node_id: int) -> None:
        self._cache.pop(node_id, None)

    async def close(self) -> None:
        for executor in self._cache.values():
            await executor.close()
        self._cache.clear()
