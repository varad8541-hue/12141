"""User facing instance commands."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .. import ui
from ..provision import Spec
from ..store import default_expiry


async def image_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    images = interaction.client.settings.images
    needle = current.lower()
    return [
        app_commands.Choice(name=label, value=ref)
        for label, ref in images
        if needle in label.lower() or needle in ref.lower()
    ][:25]


class Instances(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    vps = app_commands.Group(name="vps", description="Create and control your containers")
    ports = app_commands.Group(name="ports", description="Publish container ports on the host")

    # -------------------------------------------------------------- helpers
    def _owned(self, interaction: discord.Interaction, name: str) -> dict[str, Any] | None:
        instance = self.bot.store.instance_by_name(name)
        if instance is None or instance["state"] == "deleted":
            return None
        if int(instance["owner_id"]) != interaction.user.id and not self.bot.is_staff(interaction.user):
            return None
        return instance

    def _visible(self, interaction: discord.Interaction, name: str) -> dict[str, Any] | None:
        instance = self.bot.store.instance_by_name(name)
        if instance is None or instance["state"] == "deleted":
            return None
        if self.bot.store.is_visible_to(instance, interaction.user.id):
            return instance
        return instance if self.bot.is_staff(interaction.user) else None

    async def _deny(self, interaction: discord.Interaction, title: str, body: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=ui.fail(title, body), ephemeral=True)
        else:
            await interaction.response.send_message(embed=ui.fail(title, body), ephemeral=True)

    # --------------------------------------------------------------- commands
    @vps.command(name="list", description="Open the console for an instance")
    async def listing(self, interaction: discord.Interaction) -> None:
        store = self.bot.store
        owned = store.list_instances(interaction.user.id)
        shared = store.list_shared_instances(interaction.user.id)
        known = {item["id"] for item in owned}
        combined = owned + [item for item in shared if item["id"] not in known]

        if not combined:
            await interaction.response.send_message(
                embed=ui.info(
                    "Nothing to show",
                    "You do not own any instances yet. Staff can create one with `/vps new`.",
                ),
                ephemeral=True,
            )
            return
        await ui.show_picker(interaction, self.bot, combined)

    @vps.command(name="new", description="Request a new container")
    @app_commands.describe(
        memory="Memory in MiB",
        vcpu="Virtual CPU count",
        disk="Disk size in GiB",
        image="Image reference, see /guide",
        docker="Enable nesting so Docker works inside the container",
        privileged="Staff only, and only when the deployment allows it",
        node="Preferred node name (staff pick freely)",
    )
    @app_commands.autocomplete(image=image_autocomplete)
    async def create(
        self,
        interaction: discord.Interaction,
        memory: app_commands.Range[int, 512, 131072] = 2048,
        vcpu: app_commands.Range[int, 1, 32] = 2,
        disk: app_commands.Range[int, 5, 500] = 20,
        image: str | None = None,
        docker: bool = False,
        privileged: bool = False,
        node: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = self.bot.settings
        store = self.bot.store
        staff = self.bot.is_staff(interaction.user)

        if privileged and not (staff and settings.allow_privileged):
            await self._deny(interaction, "Not permitted", "Privileged containers are disabled here.")
            return

        if not staff and not settings.self_service:
            await self._deny(interaction, "Invite only", "Ask staff to create an instance for you.")
            return

        chosen_image = image or settings.default_image
        if chosen_image not in {ref for _, ref in settings.images}:
            await self._deny(interaction, "Unknown image", "Pick one of the images listed in `/guide`.")
            return

        quota = store.get_quota(interaction.user.id)
        usage = store.usage(interaction.user.id)
        if staff:
            if usage["count"] >= settings.max_instances_staff:
                await self._deny(interaction, "Staff cap reached", f"The staff limit is {settings.max_instances_staff} instances.")
                return
        else:
            if usage["count"] >= quota["max_instances"]:
                await self._deny(interaction, "Quota reached", f"You already have {usage['count']} of {quota['max_instances']} instances.")
                return
            if usage["memory_mb"] + memory > quota["max_memory_mb"]:
                await self._deny(interaction, "Not enough memory", f"Your plan allows {ui.human_mb(quota['max_memory_mb'])} in total.")
                return
            if usage["disk_gb"] + disk > quota["max_disk_gb"]:
                await self._deny(interaction, "Not enough disk", f"Your plan allows {quota['max_disk_gb']} GiB in total.")
                return

        target = self._pick_node(node)
        if target is None:
            await self._deny(interaction, "No capacity", "Every node is full or disabled right now.")
            return

        spec = Spec(
            image=chosen_image,
            memory_mb=int(memory),
            vcpu=int(vcpu),
            disk_gb=int(disk),
            docker=bool(docker),
            privileged=bool(privileged and staff),
        )

        async def progress(text: str) -> None:
            embed = ui.info("Provisioning", f"{text}\n\n`{spec.summary}` on **{target['name']}**")
            try:
                await interaction.edit_original_response(embed=embed)
            except discord.HTTPException:
                pass

        await progress("Queued…")
        try:
            instance = await self.bot.provisioner.create(
                owner_id=interaction.user.id,
                node_id=int(target["id"]),
                spec=spec,
                expires_at=default_expiry(settings.default_ttl_hours),
                progress=progress,
            )
        except Exception as exc:
            await interaction.edit_original_response(
                embed=ui.fail("Provisioning failed", f"`{exc}`\n\nNothing was charged against your quota.")
            )
            return

        await self.bot.audit_log(interaction.user, "instance:create", instance["name"], spec.summary)
        embed = await self._credentials_embed(instance)
        await interaction.edit_original_response(embed=embed)
        await self._email_credentials(interaction.user, instance)

    @vps.command(name="console", description="Open a console by container name")
    @app_commands.describe(name="Container name, for example u1234567890-abcd")
    async def console(self, interaction: discord.Interaction, name: str) -> None:
        instance = self._visible(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "No visible instance has that name."), ephemeral=True
            )
            return
        staff = self.bot.is_staff(interaction.user)
        await interaction.response.send_message(
            embed=ui.console_embed(self.bot.store, self.bot.provisioner, instance),
            view=ui.ConsoleView(self.bot, int(instance["id"]), interaction.user.id, staff=staff),
            ephemeral=True,
        )

    @vps.command(name="password", description="Rotate the root password")
    @app_commands.describe(name="Container name")
    async def password(self, interaction: discord.Interaction, name: str) -> None:
        instance = self._owned(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "You do not own an instance with that name."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            secret = await self.bot.provisioner.reset_password(instance)
        except Exception as exc:
            await interaction.edit_original_response(embed=ui.fail("Rotation failed", str(exc)))
            return
        await self.bot.audit_log(interaction.user, "password:rotate", name)
        await interaction.edit_original_response(
            embed=ui.ok("Password rotated", f"New root password for **{name}**:\n```{secret}```")
        )

    @vps.command(name="reconnect", description="Rebuild the SSH tunnel for an instance")
    @app_commands.describe(name="Container name")
    async def reconnect(self, interaction: discord.Interaction, name: str) -> None:
        instance = self._owned(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "You do not own an instance with that name."), ephemeral=True
            )
            return
        if instance["state"] != "running":
            await interaction.response.send_message(
                embed=ui.fail("Not running", "Start the instance before reconnecting."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            address = await self.bot.provisioner.reconnect_tunnel(instance)
        except Exception as exc:
            await interaction.edit_original_response(embed=ui.fail("Tunnel failed", str(exc)))
            return

        if not address:
            await interaction.edit_original_response(
                embed=ui.fail("Tunnel failed", "The tunnel provider did not hand out an address. Try again shortly.")
            )
            return
        host, _, port = address.partition(":")
        await interaction.edit_original_response(
            embed=ui.ok("Tunnel ready", f"```ssh root@{host} -p {port}```")
        )

    @vps.command(name="share", description="Give somebody access to an instance")
    @app_commands.describe(member="Who gets access", name="Container name")
    async def share(self, interaction: discord.Interaction, member: discord.User, name: str) -> None:
        instance = self._owned(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "You do not own an instance with that name."), ephemeral=True
            )
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                embed=ui.fail("That is you", "You already own it."), ephemeral=True
            )
            return
        self.bot.store.add_share(int(instance["id"]), member.id)
        await self.bot.audit_log(interaction.user, "share:add", name, str(member.id))
        embed = ui.ok("Shared", f"{member.mention} can open **{name}** from `/vps list`.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        try:
            await member.send(
                embed=ui.info(
                    "New shared console",
                    f"{interaction.user.mention} shared **{name}** with you. Run `/vps list` to open it.",
                )
            )
        except discord.HTTPException:
            pass

    @vps.command(name="unshare", description="Revoke somebody's access")
    @app_commands.describe(member="Who loses access", name="Container name")
    async def unshare(self, interaction: discord.Interaction, member: discord.User, name: str) -> None:
        instance = self._owned(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "You do not own an instance with that name."), ephemeral=True
            )
            return
        self.bot.store.remove_share(int(instance["id"]), member.id)
        await self.bot.audit_log(interaction.user, "share:remove", name, str(member.id))
        await interaction.response.send_message(
            embed=ui.ok("Access revoked", f"{member.mention} can no longer open **{name}**."),
            ephemeral=True,
        )

    # ----------------------------------------------------------------- ports
    @ports.command(name="add", description="Publish a container port on the host")
    @app_commands.describe(name="Container name", port="Port listening inside the container")
    async def port_add(
        self,
        interaction: discord.Interaction,
        name: str,
        port: app_commands.Range[int, 1, 65535],
    ) -> None:
        instance = self._owned(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "You do not own an instance with that name."), ephemeral=True
            )
            return
        if instance["state"] != "running":
            await interaction.response.send_message(
                embed=ui.fail("Not running", "Start the instance first."), ephemeral=True
            )
            return

        owner_id = int(instance["owner_id"])
        quota = self.bot.store.get_quota(owner_id)
        if not self.bot.is_staff(interaction.user) and self.bot.store.count_ports(owner_id) >= quota["max_ports"]:
            await interaction.response.send_message(
                embed=ui.fail("Quota reached", f"Your plan allows {quota['max_ports']} forwards."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self.bot.provisioner.add_port(instance, int(port))
        except Exception as exc:
            await interaction.edit_original_response(embed=ui.fail("Forward failed", str(exc)))
            return

        host_ip = await self.bot.provisioner.public_ip()
        await interaction.edit_original_response(
            embed=ui.ok("Forward created", f"`{host_ip}:{row['host_port']}` → `{name}:{port}` (TCP + UDP)")
        )

    @ports.command(name="list", description="List the forwards on an instance")
    @app_commands.describe(name="Container name")
    async def port_list(self, interaction: discord.Interaction, name: str) -> None:
        instance = self._visible(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "No visible instance has that name."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=ui.ports_embed(self.bot.store, self.bot.provisioner, instance), ephemeral=True
        )

    @ports.command(name="remove", description="Delete a forward by its host port")
    @app_commands.describe(name="Container name", host_port="Host port shown in /ports list")
    async def port_remove(self, interaction: discord.Interaction, name: str, host_port: int) -> None:
        instance = self._owned(interaction, name)
        if instance is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "You do not own an instance with that name."), ephemeral=True
            )
            return
        match = next(
            (row for row in self.bot.store.list_ports(int(instance["id"])) if int(row["host_port"]) == host_port),
            None,
        )
        if match is None:
            await interaction.response.send_message(
                embed=ui.fail("Not found", "No forward uses that host port."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.provisioner.remove_port(instance, match)
        except Exception as exc:
            await interaction.edit_original_response(embed=ui.fail("Removal failed", str(exc)))
            return
        await interaction.edit_original_response(
            embed=ui.ok("Forward removed", f"Host port `{host_port}` is free again.")
        )

    # ---------------------------------------------------------------- extras
    def _pick_node(self, wanted: str | None) -> dict[str, Any] | None:
        nodes = [
            node
            for node in self.bot.store.list_nodes(enabled_only=True)
            if self.bot.store.node_load(int(node["id"])) < int(node["capacity"])
        ]
        if wanted:
            needle = wanted.strip().lower()
            nodes = [
                node
                for node in nodes
                if needle in {node["name"].lower(), str(node["id"])}
            ]
        if not nodes:
            return None
        return min(nodes, key=lambda node: self.bot.store.node_load(int(node["id"])))

    async def _credentials_embed(self, instance: dict[str, Any]) -> discord.Embed:
        provisioner = self.bot.provisioner
        secret = self.bot.vault.open(instance["password_enc"])
        embed = ui.ok("Instance ready", f"**{instance['name']}** is online.")
        ui.field(embed, "Spec", f"{ui.human_mb(int(instance['memory_mb']))} · {instance['vcpu']} vCPU · {instance['disk_gb']} GiB", False)
        ui.field(embed, "Image", f"`{instance['image']}`", True)
        ui.field(embed, "Lease", provisioner.expiry_label(instance), True)

        tunnel = instance.get("tunnel") or ""
        if tunnel:
            host, _, port = tunnel.partition(":")
            ui.field(embed, "Login", f"```ssh root@{host} -p {port}```\nPassword: `{secret}`", False)
        else:
            ui.field(embed, "Login", f"Tunnel is still coming up. Root password: `{secret}`", False)
        ui.field(embed, "Next", "`/vps list` → **Login details** any time you need the credentials again.", False)
        return embed

    async def _email_credentials(self, user: discord.abc.User, instance: dict[str, Any]) -> None:
        try:
            await user.send(embed=await self._credentials_embed(instance))
        except discord.HTTPException:
            pass


async def setup(bot) -> None:
    await bot.add_cog(Instances(bot))
