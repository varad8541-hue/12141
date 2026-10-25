"""Bot wiring: shared services, extension loading and error handling."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from . import ui
from .config import Settings
from .nodes import NodeRegistry
from .provision import Provisioner
from .store import Store
from .tunnel import TunnelManager
from .vault import Vault

log = logging.getLogger("cloudbox.bot")

EXTENSIONS = (
    "cloudbox.cogs.core",
    "cloudbox.cogs.vps",
    "cloudbox.cogs.staff",
)


class CloudBoxBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.settings = settings
        self.store = Store(settings.db_path)
        self.vault = Vault(settings.key_path)
        self.registry = NodeRegistry(self.store, self.vault)
        self.tunnel = TunnelManager(settings.pinggy_token, settings.pinggy_host)
        self.provisioner = Provisioner(
            settings, self.store, self.registry, self.vault, self.tunnel
        )
        self.tree.on_error = self.on_app_error
        self._synced = False

    # ------------------------------------------------------------- lifecycle
    async def setup_hook(self) -> None:
        self.store.ensure_local_node()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        # Persistent panel survives restarts because its buttons carry custom_ids.
        self.add_view(ui.PanelView(self))

    async def on_ready(self) -> None:
        if self.user:
            log.info("signed in as %s (%s)", self.user, self.user.id)
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name="/panel · container hosting"
                )
            )
        if self._synced:
            return
        self._synced = True
        try:
            if self.settings.guild_id:
                guild = discord.Object(id=self.settings.guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("application commands synced to guild %s", self.settings.guild_id)
            else:
                await self.tree.sync()
                log.info("global application commands synced")
        except discord.HTTPException as exc:
            log.error("command sync failed: %s", exc)

    async def close(self) -> None:
        await self.registry.close()
        self.store.close()
        await super().close()

    # ---------------------------------------------------------------- helpers
    def is_staff(self, user: discord.abc.User) -> bool:
        if user.id in self.settings.owners:
            return True
        role_id = self.settings.staff_role_id
        if role_id is None:
            return False

        roles = getattr(user, "roles", None)
        if roles is not None:
            return any(role.id == role_id for role in roles)

        guild = self.get_guild(self.settings.guild_id) if self.settings.guild_id else None
        member = guild.get_member(user.id) if guild else None
        return bool(member and any(role.id == role_id for role in member.roles))

    async def audit_log(self, actor: discord.abc.User, action: str, subject: str, detail: str = "") -> None:
        self.store.audit(actor.id, action, subject, detail)
        channel_id = self.settings.audit_channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        embed = ui.card(
            "Audit",
            f"**{actor}** (`{actor.id}`) ran `{action}` on `{subject}`\n{detail}".strip(),
            "muted",
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ error hook
    async def on_app_error(self, interaction: discord.Interaction, error: Any) -> None:
        log.exception("command failed: %s", error, exc_info=error)
        message = "Something broke on my side. The error was logged."
        if isinstance(error, discord.Forbidden):
            message = "I do not have permission to do that here."
        elif isinstance(error, discord.HTTPException):
            message = "Discord rejected the request. Try again in a moment."

        embed = ui.fail("Unhandled error", message)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass
