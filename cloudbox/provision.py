"""Container lifecycle: create, rebuild, power, password, ports."""

from __future__ import annotations

import asyncio
import random
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

from .config import Settings
from .nodes import Executor, NodeError, NodeRegistry
from .store import Store, parse_stamp, stamp
from .tunnel import TunnelManager
from .vault import Vault, new_password

Progress = Callable[[str], Awaitable[None]]

PRIVILEGED_RAW = r"""lxc.apparmor.profile = unconfined
lxc.cap.drop =
lxc.cgroup2.devices.allow = a
lxc.mount.auto = proc:rw sys:rw cgroup:rw
"""


class ProvisionError(RuntimeError):
    pass


@dataclass(slots=True)
class Spec:
    image: str
    memory_mb: int
    vcpu: int
    disk_gb: int
    docker: bool = False
    privileged: bool = False

    @property
    def summary(self) -> str:
        return f"{self.memory_mb // 1024} GiB / {self.vcpu} vCPU / {self.disk_gb} GiB"


BOOTSTRAP = r"""
set -u
export DEBIAN_FRONTEND=noninteractive

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq openssh-server >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q openssh-server >/dev/null 2>&1 || true
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache openssh >/dev/null 2>&1 || true
fi

mkdir -p /etc/sysctl.d /run/sshd
cat > /etc/sysctl.d/90-cloudbox.conf <<'EOF'
net.ipv4.ip_unprivileged_port_start=0
net.ipv4.ping_group_range=0 2147483647
fs.inotify.max_user_watches=524288
EOF
sysctl -p /etc/sysctl.d/90-cloudbox.conf >/dev/null 2>&1 || true

mkdir -p /etc/ssh/sshd_config.d
printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' \
  > /etc/ssh/sshd_config.d/99-cloudbox.conf
sed -i -E 's/^#?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || true
sed -i -E 's/^#?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true
service ssh restart >/dev/null 2>&1 \
  || service sshd restart >/dev/null 2>&1 \
  || systemctl restart ssh >/dev/null 2>&1 \
  || /usr/sbin/sshd >/dev/null 2>&1 || true
"""


class Provisioner:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        registry: NodeRegistry,
        vault: Vault,
        tunnel: TunnelManager,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.vault = vault
        self.tunnel = tunnel
        self._ip: str | None = settings.public_ip or None

    # ------------------------------------------------------------- utilities
    async def public_ip(self) -> str:
        if self._ip:
            return self._ip
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
                async with session.get("https://api.ipify.org") as response:
                    self._ip = (await response.text()).strip()
        except Exception:
            self._ip = ""
        return self._ip or "server-ip-unavailable"

    def executor(self, instance: dict[str, Any]) -> Executor:
        node = self.store.get_node(int(instance["node_id"]))
        if not node:
            raise ProvisionError("the node hosting this instance no longer exists")
        return self.registry.executor_for(node)

    async def _lxc(
        self, instance: dict[str, Any], *argv: str, timeout: float = 120.0
    ) -> str:
        executor = self.executor(instance)
        result = await executor.run(list(argv), timeout=timeout)
        if not result.ok:
            raise ProvisionError(result.err or result.out or "lxc command failed")
        return result.out

    async def _wait_ready(
        self, instance: dict[str, Any], timeout: float = 150.0
    ) -> None:
        executor = self.executor(instance)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await executor.run(
                ["exec", instance["name"], "--", "true"], timeout=20
            )
            if result.ok:
                return
            await asyncio.sleep(2)
        raise ProvisionError("the container did not finish booting in time")

    async def _apply_profile(self, instance: dict[str, Any], spec: Spec) -> None:
        name = instance["name"]
        if spec.docker:
            await self._lxc(instance, "config", "set", name, "security.nesting", "true")
            await self._lxc(
                instance, "config", "set", name, "security.syscalls.intercept.mknod", "true"
            )
            await self._lxc(
                instance, "config", "set", name, "security.syscalls.intercept.setxattr", "true"
            )
        if spec.privileged:
            await self._lxc(instance, "config", "set", name, "security.privileged", "true")
            await self._lxc(instance, "config", "set", name, "security.nesting", "true")
            await self._lxc(instance, "config", "set", name, "raw.lxc", PRIVILEGED_RAW)

    async def _provision_fresh(
        self, instance: dict[str, Any], spec: Spec, progress: Progress
    ) -> tuple[str, str | None]:
        node = self.store.get_node(int(instance["node_id"]))
        assert node is not None
        executor = self.registry.executor_for(node)
        name = instance["name"]

        await progress("Allocating filesystem and pulling the image…")
        launch = [
            "init",
            spec.image,
            name,
            "--storage",
            self.settings.storage_pool,
            "--device",
            f"root,size={spec.disk_gb}GB",
        ]
        result = await executor.run(launch, timeout=600)
        if not result.ok:
            raise ProvisionError(result.err or "could not create the container")

        await self._lxc(instance, "config", "set", name, "limits.memory", f"{spec.memory_mb}MiB")
        await self._lxc(instance, "config", "set", name, "limits.cpu", str(spec.vcpu))
        await self._lxc(instance, "config", "set", name, "boot.autostart", "true")

        await progress("Applying isolation profile…")
        await self._apply_profile(instance, spec)

        started = await executor.run(["start", name], timeout=180)
        if not started.ok and "already" not in (started.err or "").lower():
            raise ProvisionError(started.err or "could not start the container")

        await progress("Waiting for the guest to boot…")
        await self._wait_ready(instance)

        await progress("Installing the SSH daemon…")
        boot = await executor.run(
            ["exec", name, "--", "sh", "-lc", BOOTSTRAP], timeout=900
        )
        if not boot.ok:
            raise ProvisionError(boot.err or "guest bootstrap failed")

        password = new_password()
        quoted = password.replace("'", "'\\''")
        changed = await executor.run(
            ["exec", name, "--", "sh", "-lc", f"echo 'root:{quoted}' | chpasswd"],
            timeout=60,
        )
        if not changed.ok:
            raise ProvisionError(changed.err or "could not set the root password")

        await progress("Opening a reverse tunnel…")
        try:
            address = await self.tunnel.open(executor, name)
        except NodeError:
            address = None

        return password, address

    # ---------------------------------------------------------------- create
    async def create(
        self,
        owner_id: int,
        node_id: int,
        spec: Spec,
        expires_at: str | None,
        progress: Progress,
    ) -> dict[str, Any]:
        name = self._unique_name(owner_id)
        instance_id = self.store.create_instance(
            owner_id=owner_id,
            node_id=node_id,
            name=name,
            image=spec.image,
            memory_mb=spec.memory_mb,
            vcpu=spec.vcpu,
            disk_gb=spec.disk_gb,
            docker=spec.docker,
            privileged=spec.privileged,
            expires_at=expires_at,
        )
        instance = self.store.get_instance(instance_id)
        assert instance is not None

        try:
            password, address = await self._provision_fresh(instance, spec, progress)
        except Exception:
            # Do not leave half-built containers (and their disk usage) behind.
            try:
                executor = self.executor(instance)
                await executor.run(["delete", instance["name"], "--force"], timeout=120)
            except Exception:
                pass
            self.store.update_instance(instance_id, state="error")
            raise

        self.store.update_instance(
            instance_id,
            state="running",
            password_enc=self.vault.seal(password),
            tunnel=address or "",
        )
        fresh = self.store.get_instance(instance_id)
        assert fresh is not None
        return fresh

    async def rebuild(self, instance: dict[str, Any], image: str, progress: Progress) -> None:
        spec = self.spec_of(instance)
        spec.image = image
        self.store.update_instance(instance["id"], state="rebuilding", tunnel="")

        await progress("Destroying the old root filesystem…")
        executor = self.executor(instance)
        await executor.run(["delete", instance["name"], "--force"], timeout=180)

        try:
            await progress(f"Deploying {image}…")
            password, address = await self._provision_fresh(instance, spec, progress)
        except Exception:
            self.store.update_instance(instance["id"], state="error", tunnel="")
            raise

        for row in self.store.list_ports(int(instance["id"])):
            await self._attach_port(instance, row["host_port"], row["guest_port"])

        self.store.update_instance(
            instance["id"],
            state="running",
            image=image,
            suspended=0,
            password_enc=self.vault.seal(password),
            tunnel=address or "",
        )

    @staticmethod
    def spec_of(instance: dict[str, Any]) -> Spec:
        return Spec(
            image=instance["image"],
            memory_mb=int(instance["memory_mb"]),
            vcpu=int(instance["vcpu"]),
            disk_gb=int(instance["disk_gb"]),
            docker=bool(instance["docker"]),
            privileged=bool(instance["privileged"]),
        )

    # ----------------------------------------------------------------- power
    async def start(self, instance: dict[str, Any]) -> str | None:
        executor = self.executor(instance)
        result = await executor.run(["start", instance["name"]], timeout=180)
        if not result.ok and "already" not in (result.err or "").lower():
            raise ProvisionError(result.err or "could not start the instance")

        self.store.update_instance(instance["id"], state="running", suspended=0)
        for row in self.store.list_ports(int(instance["id"])):
            await self._attach_port(instance, row["host_port"], row["guest_port"])

        address = None
        try:
            address = await self.tunnel.open(executor, instance["name"])
        except NodeError:
            address = None
        self.store.update_instance(instance["id"], tunnel=address or "")
        return address

    async def stop(self, instance: dict[str, Any]) -> None:
        executor = self.executor(instance)
        result = await executor.run(["stop", instance["name"]], timeout=180)
        if not result.ok and "not running" not in (result.err or "").lower():
            raise ProvisionError(result.err or "could not stop the instance")
        self.store.update_instance(instance["id"], state="stopped", tunnel="")

    async def restart(self, instance: dict[str, Any]) -> str | None:
        executor = self.executor(instance)
        result = await executor.run(["restart", instance["name"]], timeout=240)
        if not result.ok:
            raise ProvisionError(result.err or "could not restart the instance")
        address = None
        try:
            address = await self.tunnel.open(executor, instance["name"])
        except NodeError:
            address = None
        self.store.update_instance(instance["id"], state="running", suspended=0, tunnel=address or "")
        return address

    async def destroy(self, instance: dict[str, Any]) -> None:
        executor = self.executor(instance)
        result = await executor.run(["delete", instance["name"], "--force"], timeout=240)
        if not result.ok:
            lowered = (result.err or "").lower()
            if "not found" not in lowered and "no such" not in lowered:
                raise ProvisionError(result.err or "could not delete the instance")
        for row in self.store.list_ports(int(instance["id"])):
            self.store.delete_port(int(row["id"]))
        self.store.update_instance(instance["id"], state="deleted", tunnel="")

    # -------------------------------------------------------------- password
    async def reset_password(self, instance: dict[str, Any]) -> str:
        password = new_password()
        quoted = password.replace("'", "'\\''")
        executor = self.executor(instance)
        result = await executor.run(
            [
                "exec",
                instance["name"],
                "--",
                "sh",
                "-lc",
                f"echo 'root:{quoted}' | chpasswd",
            ],
            timeout=60,
        )
        if not result.ok:
            raise ProvisionError(result.err or "could not change the guest password")
        self.store.update_instance(instance["id"], password_enc=self.vault.seal(password))
        return password

    async def reconnect_tunnel(self, instance: dict[str, Any]) -> str | None:
        executor = self.executor(instance)
        address = await self.tunnel.open(executor, instance["name"])
        self.store.update_instance(instance["id"], tunnel=address or "")
        return address

    # ----------------------------------------------------------------- ports
    def _pick_host_port(self) -> int:
        low, high = self.settings.port_range
        for _ in range(250):
            candidate = random.randint(low, high)
            if not self.store.host_port_taken(candidate):
                return candidate
        raise ProvisionError("no free host ports left in the configured range")

    async def _attach_port(self, instance: dict[str, Any], host_port: int, guest_port: int) -> None:
        name = instance["name"]
        await self._lxc(
            instance,
            "config",
            "device",
            "add",
            name,
            f"tcp-{host_port}",
            "proxy",
            f"listen=tcp:0.0.0.0:{host_port}",
            f"connect=tcp:127.0.0.1:{guest_port}",
        )
        try:
            await self._lxc(
                instance,
                "config",
                "device",
                "add",
                name,
                f"udp-{host_port}",
                "proxy",
                f"listen=udp:0.0.0.0:{host_port}",
                f"connect=udp:127.0.0.1:{guest_port}",
            )
        except ProvisionError:
            pass

    async def add_port(self, instance: dict[str, Any], guest_port: int) -> dict[str, Any]:
        host_port = self._pick_host_port()
        await self._attach_port(instance, host_port, guest_port)
        port_id = self.store.add_port(int(instance["id"]), host_port, guest_port)
        row = self.store.get_port(port_id)
        assert row is not None
        return row

    async def remove_port(self, instance: dict[str, Any], port: dict[str, Any]) -> None:
        name = instance["name"]
        for proto in ("tcp", "udp"):
            try:
                await self._lxc(
                    instance, "config", "device", "remove", name, f"{proto}-{port['host_port']}"
                )
            except ProvisionError:
                pass
        self.store.delete_port(int(port["id"]))

    # ---------------------------------------------------------------- naming
    def _unique_name(self, owner_id: int) -> str:
        for _ in range(30):
            candidate = f"u{owner_id}-{secrets.token_hex(2)}"
            if self.store.instance_by_name(candidate) is None:
                return candidate
        raise ProvisionError("could not allocate a unique container name")

    # --------------------------------------------------------------- helpers
    @staticmethod
    def expiry_label(instance: dict[str, Any]) -> str:
        moment = parse_stamp(instance.get("expires_at"))
        if moment is None:
            return "never"
        return f"<t:{int(moment.timestamp())}:R>"

    def is_expired(self, instance: dict[str, Any]) -> bool:
        moment = parse_stamp(instance.get("expires_at"))
        return moment is not None and moment < parse_stamp(stamp())
