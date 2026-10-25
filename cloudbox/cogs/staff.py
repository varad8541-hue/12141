"""Operator commands: nodes, quotas, moderation and audit."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .. import ui
from ..store import parse_stamp, stamp, utcnow
from ..vault import new_node_secret


class Staff(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    staff = app_commands.Group(name="staff", description="Operator tools")

    # -------------------------------------------------------------- helpers
    async def _guard(self, interaction: discord.Interaction) -> bool:
        if self.bot.is_staff(interaction.user):
            return True
        await interaction.response.send_message(
            embed=ui.fail("Staff only", "This command is restricted to operators."), ephemeral=True
        )
        return False

    def _find(self, name: str) -> dict[str, Any] | None:
        instance = self.bot.store.instance_by_name(name)
        if instance is None or instance["state"] == "deleted":
            return None
        return instance

    # ----------------------------------------------------------------- nodes
    @staff.command(name="node-add", description="Register a node (empty endpoint = this host)")
    @app_commands.describe(
        name="Unique node name",
        region="Free-form location label",
        endpoint="Base URL of a node agent, e.g. https://node.example.com",
        capacity="How many instances the node may host",
    )
    async def node_add(
        self,
        interaction: discord.Interaction,
        name: str,
        region: str = "",
        endpoint: str = "",
        capacity: app_commands.Range[int, 1, 500] = 20,
    ) -> None:
        if not await self._guard(interaction):
            return

        store = self.bot.store
        if store.node_by_name(name):
            await interaction.response.send_message(
                embed=ui.fail("Name taken", "Pick a different node name."), ephemeral=True
            )
            return

        endpoint = endpoint.strip()
        if endpoint and not endpoint.startswith(("http://", "https://")):
            await interaction.response.send_message(
                embed=ui.fail("Bad endpoint", "The endpoint must start with http:// or https://."),
                ephemeral=True,
            )
            return

        if endpoint:
            kind = "remote"
            secret = new_node_secret()
            sealed = self.bot.vault.seal(secret)
        else:
            if any(node["kind"] == "local" for node in store.list_nodes()):
                await interaction.response.send_message(
                    embed=ui.fail("Local node exists", "This host is already registered as a node."),
                    ephemeral=True,
                )
                return
            kind = "local"
            secret = ""
            sealed = ""

        node_id = store.add_node(name, region, kind, endpoint, sealed, int(capacity))
        await self.bot.audit_log(interaction.user, "node:add", name, kind)

        embed = ui.ok("Node registered", f"**{name}** is node `#{node_id}` ({kind}).")
        ui.field(embed, "Region", region or "—", True)
        ui.field(embed, "Capacity", str(capacity), True)
        if endpoint:
            ui.field(embed, "Endpoint", endpoint, False)
            ui.field(
                embed,
                "Agent secret (shown once)",
                f"```{secret}```\nStart the agent with `AGENT_SECRET` set to this value.",
                False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staff.command(name="node-list", description="List nodes and their load")
    async def node_list(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        nodes = self.bot.store.list_nodes()
        embed = ui.info("Nodes", f"{len(nodes)} registered")
        for node in nodes:
            load = self.bot.store.node_load(int(node["id"]))
            ui.field(
                embed,
                f"#{node['id']} · {node['name']}",
                f"`{node['kind']}` · {node['region'] or 'n/a'}\n"
                f"load **{load}/{node['capacity']}** · "
                f"{'enabled' if node['enabled'] else 'disabled'}",
                True,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staff.command(name="node-remove", description="Remove an empty node")
    async def node_remove(self, interaction: discord.Interaction, node_id: int) -> None:
        if not await self._guard(interaction):
            return
        node = self.bot.store.get_node(node_id)
        if node is None:
            await interaction.response.send_message(embed=ui.fail("Not found", "No such node."), ephemeral=True)
            return
        load = self.bot.store.node_load(node_id)
        if load:
            await interaction.response.send_message(
                embed=ui.fail("Node busy", f"It still hosts {load} instance(s). Remove them first."),
                ephemeral=True,
            )
            return
        self.bot.store.delete_node(node_id)
        self.bot.registry.invalidate(node_id)
        await self.bot.audit_log(interaction.user, "node:remove", node["name"])
        await interaction.response.send_message(
            embed=ui.ok("Removed", f"Node **{node['name']}** is gone."), ephemeral=True
        )

    @staff.command(name="node-toggle", description="Enable or disable a node")
    async def node_toggle(self, interaction: discord.Interaction, node_id: int, enabled: bool) -> None:
        if not await self._guard(interaction):
            return
        node = self.bot.store.get_node(node_id)
        if node is None:
            await interaction.response.send_message(embed=ui.fail("Not found", "No such node."), ephemeral=True)
            return
        self.bot.store.set_node_enabled(node_id, enabled)
        await self.bot.audit_log(interaction.user, "node:toggle", node["name"], str(enabled))
        await interaction.response.send_message(
            embed=ui.ok("Updated", f"**{node['name']}** is now {'enabled' if enabled else 'disabled'}."),
            ephemeral=True,
        )

    # ---------------------------------------------------------------- quotas
    @staff.command(name="quota-set", description="Set a member's limits")
    @app_commands.describe(
        member="Whose limits change",
        instances="Maximum number of containers",
        memory="Total memory in MiB",
        disk="Total disk in GiB",
        ports="Maximum port forwards",
    )
    async def quota_set(
        self,
        interaction: discord.Interaction,
        member: discord.User,
        instances: app_commands.Range[int, 0, 100] = 2,
        memory: app_commands.Range[int, 256, 1048576] = 4096,
        disk: app_commands.Range[int, 5, 5000] = 40,
        ports: app_commands.Range[int, 0, 200] = 4,
    ) -> None:
        if not await self._guard(interaction):
            return
        self.bot.store.set_quota(member.id, int(instances), int(memory), int(disk), int(ports))
        await self.bot.audit_log(
            interaction.user, "quota:set", str(member.id),
            f"{instances} inst / {memory} MiB / {disk} GiB / {ports} ports",
        )
        embed = ui.ok("Quota saved", f"{member.mention} now has:")
        ui.field(embed, "Instances", str(instances), True)
        ui.field(embed, "Memory", ui.human_mb(int(memory)), True)
        ui.field(embed, "Disk", f"{disk} GiB", True)
        ui.field(embed, "Ports", str(ports), True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staff.command(name="quota-show", description="Inspect a member's usage")
    async def quota_show(self, interaction: discord.Interaction, member: discord.User) -> None:
        if not await self._guard(interaction):
            return
        embed = ui.usage_embed(self.bot.store, member.id)
        embed.title = f"ℹ️  Usage · {member}"
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------- moderation
    @staff.command(name="suspend", description="Stop and lock an instance")
    async def suspend(self, interaction: discord.Interaction, name: str, *, reason: str = "staff action") -> None:
        if not await self._guard(interaction):
            return
        instance = self._find(name)
        if instance is None:
            await interaction.response.send_message(embed=ui.fail("Not found", "No such instance."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.provisioner.stop(instance)
        except Exception as exc:
            await interaction.edit_original_response(embed=ui.fail("Suspend failed", str(exc)))
            return
        self.bot.store.update_instance(instance["id"], suspended=1, tunnel="")
        await self.bot.audit_log(interaction.user, "instance:suspend", name, reason)
        await interaction.edit_original_response(embed=ui.ok("Locked", f"**{name}** is stopped and locked."))
        await self._notify(
            int(instance["owner_id"]),
            ui.warn("Instance locked", f"**{name}** was locked.\n**Reason:** {reason}"),
        )

    @staff.command(name="unsuspend", description="Unlock and start an instance")
    async def unsuspend(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guard(interaction):
            return
        instance = self._find(name)
        if instance is None:
            await interaction.response.send_message(embed=ui.fail("Not found", "No such instance."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.bot.store.update_instance(instance["id"], suspended=0)
        try:
            await self.bot.provisioner.start(instance)
        except Exception as exc:
            await interaction.edit_original_response(embed=ui.fail("Start failed", str(exc)))
            return
        await self.bot.audit_log(interaction.user, "instance:unsuspend", name)
        await interaction.edit_original_response(embed=ui.ok("Unlocked", f"**{name}** is running again."))
        await self._notify(
            int(instance["owner_id"]),
            ui.ok("Instance unlocked", f"**{name}** is available again."),
        )

    @staff.command(name="extend", description="Extend an instance lease")
    async def extend(
        self, interaction: discord.Interaction, name: str, hours: app_commands.Range[int, 1, 8760]
    ) -> None:
        if not await self._guard(interaction):
            return
        instance = self._find(name)
        if instance is None:
            await interaction.response.send_message(embed=ui.fail("Not found", "No such instance."), ephemeral=True)
            return
        current = parse_stamp(instance.get("expires_at"))
        base = current if current and current > utcnow() else utcnow()
        new_expiry = stamp(base + timedelta(hours=int(hours)))
        self.bot.store.update_instance(instance["id"], expires_at=new_expiry, suspended=0)
        await self.bot.audit_log(interaction.user, "instance:extend", name, f"{hours}h")
        embed = ui.ok("Lease extended", f"**{name}** now expires <t:{int(parse_stamp(new_expiry).timestamp())}:R>.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staff.command(name="inspect", description="Everything about one instance")
    async def inspect(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._guard(interaction):
            return
        instance = self._find(name)
        if instance is None:
            await interaction.response.send_message(embed=ui.fail("Not found", "No such instance."), ephemeral=True)
            return
        node = self.bot.store.get_node(int(instance["node_id"]))
        shares = self.bot.store.list_shares(int(instance["id"]))
        ports = self.bot.store.list_ports(int(instance["id"]))

        embed = ui.card(f"🔎  {name}", f"Owner <@{instance['owner_id']}>", "muted")
        ui.field(embed, "State", ui.state_label(instance), True)
        ui.field(embed, "Node", node["name"] if node else "missing", True)
        ui.field(embed, "Image", f"`{instance['image']}`", True)
        ui.field(embed, "Spec", f"{ui.human_mb(int(instance['memory_mb']))} · {instance['vcpu']} vCPU · {instance['disk_gb']} GiB", False)
        ui.field(embed, "Created", f"<t:{int(parse_stamp(instance['created_at']).timestamp())}:f>", True)
        ui.field(embed, "Lease", self.bot.provisioner.expiry_label(instance), True)
        ui.field(embed, "Tunnel", f"`{instance['tunnel']}`" if instance.get("tunnel") else "down", True)
        ui.field(embed, "Shared with", ", ".join(f"<@{uid}>" for uid in shares) or "nobody", False)
        if ports:
            ui.field(embed, "Forwards", "\n".join(f"`{p['host_port']}` → `{p['guest_port']}`" for p in ports), False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staff.command(name="audit", description="Recent operator actions")
    async def audit(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        rows = self.bot.store.recent_audit(15)
        if not rows:
            await interaction.response.send_message(embed=ui.info("Audit", "Nothing recorded yet."), ephemeral=True)
            return
        lines = [
            f"<t:{int(parse_stamp(row['created_at']).timestamp())}:R> · <@{row['actor_id']}> · "
            f"`{row['action']}` · `{row['subject']}`"
            + (f" · {row['detail']}" if row["detail"] else "")
            for row in rows
        ]
        embed = ui.info("Audit trail", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------- helper
    async def _notify(self, user_id: int, embed: discord.Embed) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
        except Exception:
            pass


async def setup(bot) -> None:
    await bot.add_cog(Staff(bot))
