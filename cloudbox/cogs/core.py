"""Core commands: panel, health, guide, and the expiry reaper."""

from __future__ import annotations

import asyncio
import logging
import shutil

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import ui
from ..nodes import RemoteExecutor

log = logging.getLogger("cloudbox.core")


class Core(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reaper.start()

    async def cog_unload(self) -> None:
        self.reaper.cancel()

    # -------------------------------------------------------------- commands
    @app_commands.command(name="ping", description="Round-trip time to Discord")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            embed=ui.ok("Alive", f"Gateway latency: **{latency} ms**"), ephemeral=True
        )

    @app_commands.command(name="guide", description="How the hosting panel works")
    async def guide(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=ui.guide_embed(self.bot.settings.images), ephemeral=True
        )

    @app_commands.command(name="panel", description="Post the control panel (staff only)")
    async def panel(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_staff(interaction.user):
            await interaction.response.send_message(
                embed=ui.fail("Staff only", "You cannot post the panel."), ephemeral=True
            )
            return

        embed = ui.card(
            "Control panel",
            "Self-service container hosting. Open a console to manage a box you own "
            "or one that somebody shared with you.",
            "info",
            footer=f"{self.bot.settings.brand} · root SSH inside an isolated container",
        )
        ui.field(embed, "Access", "**Login details** shows your live tunnel and root password.")
        ui.field(embed, "Fair use", "Instances expire automatically when their lease ends.")
        await interaction.response.send_message(embed=embed, view=ui.PanelView(self.bot))

    @app_commands.command(name="status", description="Control-plane and host health")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        store = self.bot.store
        instances = store.list_all_instances()
        running = sum(1 for item in instances if item["state"] == "running")
        nodes = store.list_nodes()

        embed = ui.info("Status", f"`{self.bot.settings.brand}` control plane")
        ui.field(
            embed,
            "Instances",
            f"{len(instances)} total · {running} running · {len(nodes)} node(s)",
            True,
        )
        ui.field(embed, "Gateway", f"{round(self.bot.latency * 1000)} ms", True)

        host = await asyncio.to_thread(_host_snapshot)
        if host is not None:
            ui.field(
                embed,
                "Host CPU",
                f"{ui.meter(host['cpu'])} {host['cpu']:.0f}%",
                False,
            )
            ui.field(
                embed,
                "Host memory",
                f"{ui.meter(host['mem_pct'])} {host['mem_used']}/{host['mem_total']} GiB",
                False,
            )
            ui.field(embed, "Host disk", host["disk"], False)

        for node in nodes:
            load = store.node_load(int(node["id"]))
            detail = f"{node['kind']} · {node['region'] or 'n/a'} · {load}/{node['capacity']} slots"
            executor = self.bot.registry.executor_for(node)
            if isinstance(executor, RemoteExecutor):
                try:
                    remote = await executor.stats()
                    detail += (
                        f"\nCPU {float(remote.get('cpu_pct', 0)):.0f}%"
                        f" · RAM {float(remote.get('mem_pct', 0)):.0f}%"
                    )
                except Exception:
                    detail += "\n⚠️ agent unreachable"
            ui.field(embed, f"Node · {node['name']}", detail, True)

        await interaction.edit_original_response(embed=embed)

    # ----------------------------------------------------------------- task
    @tasks.loop(minutes=5)
    async def reaper(self) -> None:
        for instance in self.bot.store.expired_instances():
            try:
                await self.bot.provisioner.stop(instance)
                self.bot.store.update_instance(instance["id"], suspended=1, tunnel="")
                self.bot.store.audit(0, "lease:expire", instance["name"])
            except Exception as exc:  # keep the loop alive no matter what
                log.warning("could not expire %s: %s", instance["name"], exc)
                continue

            try:
                owner = self.bot.get_user(int(instance["owner_id"])) or await self.bot.fetch_user(
                    int(instance["owner_id"])
                )
                await owner.send(
                    embed=ui.warn(
                        "Lease ended",
                        f"**{instance['name']}** was stopped because its lease ran out.\n"
                        "Ask staff for an extension, or rebuild it from the panel.",
                    )
                )
            except Exception:
                pass

    @reaper.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


def _host_snapshot() -> dict | None:
    try:
        import psutil
    except ImportError:
        return None

    memory = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    return {
        "cpu": psutil.cpu_percent(interval=0.5),
        "mem_pct": memory.percent,
        "mem_used": round(memory.used / 1024**3, 1),
        "mem_total": round(memory.total / 1024**3, 1),
        "disk": f"{disk.used / 1024**3:.1f}/{disk.total / 1024**3:.1f} GiB "
        f"({disk.used / disk.total * 100:.0f}%)",
    }


async def setup(bot) -> None:
    await bot.add_cog(Core(bot))
