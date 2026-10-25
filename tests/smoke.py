"""Offline smoke test: no Discord connection, no LXD required.

    python tests/smoke.py

It boots the bot in-process, registers every slash command, then drives the
provisioner against a fake executor so the create → port → destroy path is
exercised end to end.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DISCORD_TOKEN", "smoke-test-token")
os.environ.setdefault("CLOUDBOX_DATA", tempfile.mkdtemp(prefix="cloudbox-smoke-"))
os.environ.setdefault("GUILD_ID", "123456789012345678")
os.environ.setdefault("OWNER_IDS", "42")

from cloudbox import ui  # noqa: E402
from cloudbox.bot import CloudBoxBot  # noqa: E402
from cloudbox.config import load_settings  # noqa: E402
from cloudbox.nodes import Result  # noqa: E402


class FakeExecutor:
    kind = "local"
    label = "fake"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, argv, timeout: float = 120.0) -> Result:
        self.calls.append(list(argv))
        return Result(0, "", "")

    async def close(self) -> None:
        return None


class StubTunnel:
    async def open(self, executor, container, attempts: int = 8, delay: float = 3.0):
        return "tunnel.example.net:22 listen.example.net:51234"


async def main() -> None:
    settings = load_settings()
    bot = CloudBoxBot(settings)
    fake = FakeExecutor()
    bot.registry._cache[int(bot.store.ensure_local_node()["id"])] = fake

    await bot.setup_hook()
    commands = sorted(command.qualified_name for command in bot.tree.walk_commands())
    print(f"registered {len(commands)} commands")
    for name in commands:
        print("  /" + name)
    assert {"vps new", "vps list", "ports add", "staff node-add", "panel"} <= set(commands)

    provisioner = bot.provisioner
    provisioner.tunnel = StubTunnel()

    spec = provisioner.spec_of(
        {
            "image": settings.default_image,
            "memory_mb": 2048,
            "vcpu": 2,
            "disk_gb": 20,
            "docker": False,
            "privileged": False,
        }
    )

    events: list[str] = []

    async def progress(text: str) -> None:
        events.append(text)

    instance = await provisioner.create(42, 1, spec, None, progress)
    print(f"created {instance['name']} state={instance['state']} tunnel={instance['tunnel']}")
    assert instance["state"] == "running"
    assert instance["tunnel"].startswith("tunnel.example.net")
    assert bot.vault.open(instance["password_enc"])
    assert len(bot.store.list_instances(42)) == 1
    assert events, "progress callbacks never fired"

    # Presentation layer: the console must hide destructive controls from
    # people who merely share a box.
    owner_view = ui.ConsoleView(bot, int(instance["id"]), 42)
    stranger_view = ui.ConsoleView(bot, int(instance["id"]), 77)
    staff_view = ui.ConsoleView(bot, int(instance["id"]), 77, staff=True)
    assert len(owner_view.children) == 9, len(owner_view.children)
    assert len(stranger_view.children) == 6, len(stranger_view.children)
    assert len(staff_view.children) == 9, len(staff_view.children)

    bot.store.add_share(int(instance["id"]), 77)
    shared_view = ui.ConsoleView(bot, int(instance["id"]), 77)
    assert len(shared_view.children) == 6

    ui.console_embed(bot.store, bot.provisioner, instance)
    ui.usage_embed(bot.store, 42)
    ui.guide_embed(settings.images)
    assert len(ui.PanelView(bot).children) == 3
    ui.TypedConfirmModal("Rebuild", instance, lambda: None)
    ui.PortsView(bot, int(instance["id"]), 42)
    ui.ImageChoice(bot, int(instance["id"]))

    port = await provisioner.add_port(instance, 8080)
    assert port["guest_port"] == 8080
    assert bot.store.count_ports(42) == 1
    await provisioner.remove_port(instance, port)
    assert bot.store.count_ports(42) == 0

    instance = bot.store.get_instance(int(instance["id"]))
    assert instance is not None
    await provisioner.stop(instance)
    assert bot.store.get_instance(int(instance["id"]))["state"] == "stopped"

    await provisioner.start(instance)
    await provisioner.destroy(bot.store.get_instance(int(instance["id"])))

    usage = bot.store.usage(42)
    print(f"usage after destroy: {usage}")
    assert usage["count"] == 0

    core = bot.get_cog("Core")
    if core is not None:
        await core.cog_unload()

    await bot.registry.close()
    bot.store.close()
    print("\nsmoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
