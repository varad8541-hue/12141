"""Discord presentation: embeds, the persistent panel and the per-instance console."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import discord

from .provision import Provisioner
from .store import Store

TONE = {
    "ok": 0x3BA55D,
    "info": 0x5865F2,
    "warn": 0xFAA61A,
    "err": 0xED4245,
    "muted": 0x2B2D31,
}

STATE_MARK = {
    "running": "🟢",
    "stopped": "⚪",
    "provisioning": "🟡",
    "rebuilding": "🟡",
    "error": "🔴",
    "deleted": "⚫",
}


# --------------------------------------------------------------------- embeds
def card(title: str, body: str = "", tone: str = "info", *, footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=title[:256],
        description=body[:4096] or None,
        color=TONE.get(tone, TONE["info"]),
        timestamp=datetime.now(timezone.utc),
    )
    if footer:
        embed.set_footer(text=footer[:2048])
    return embed


def ok(title: str, body: str = "") -> discord.Embed:
    return card(f"✅  {title}", body, "ok")


def info(title: str, body: str = "") -> discord.Embed:
    return card(f"ℹ️  {title}", body, "info")


def warn(title: str, body: str = "") -> discord.Embed:
    return card(f"⚠️  {title}", body, "warn")


def fail(title: str, body: str = "") -> discord.Embed:
    return card(f"⛔  {title}", body, "err")


def field(embed: discord.Embed, name: str, value: str, inline: bool = False) -> discord.Embed:
    embed.add_field(name=name[:256], value=(value or "—")[:1024], inline=inline)
    return embed


def meter(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)


def human_mb(value: int) -> str:
    if value >= 1024:
        return f"{value / 1024:g} GiB"
    return f"{value} MiB"


def state_label(instance: dict[str, Any]) -> str:
    state = str(instance.get("state", "unknown"))
    mark = STATE_MARK.get(state, "❔")
    label = f"{mark} {state.upper()}"
    if instance.get("suspended"):
        label += " · locked"
    return label


def console_embed(store: Store, provisioner: Provisioner, instance: dict[str, Any]) -> discord.Embed:
    node = store.get_node(int(instance["node_id"]))
    node_name = node["name"] if node else "unknown"
    embed = card(
        f"{instance['name']}",
        f"Managed on **{node_name}** · created <t:{_epoch(instance['created_at'])}:R>",
        "ok" if instance["state"] == "running" and not instance["suspended"] else "muted",
    )
    field(embed, "State", state_label(instance), True)
    field(embed, "Memory", human_mb(int(instance["memory_mb"])), True)
    field(embed, "vCPU", str(instance["vcpu"]), True)
    field(embed, "Disk", f"{instance['disk_gb']} GiB", True)
    field(embed, "Image", f"`{instance['image']}`", True)
    field(embed, "Expires", provisioner.expiry_label(instance), True)

    tunnel = instance.get("tunnel") or ""
    if tunnel:
        host, _, port = tunnel.partition(":")
        field(embed, "Endpoint", f"`ssh root@{host} -p {port}`", False)
    else:
        field(embed, "Endpoint", "no tunnel — use **Login details** or restart the instance", False)

    if instance.get("suspended"):
        field(embed, "Notice", "An operator locked this instance. Contact staff.", False)
    return embed


def _epoch(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return 0


def usage_embed(store: Store, user_id: int) -> discord.Embed:
    quota = store.get_quota(user_id)
    usage = store.usage(user_id)
    ports = store.count_ports(user_id)
    embed = info("Your allocation", "Limits are enforced when you request a new instance.")
    field(embed, "Instances", f"{usage['count']} / {quota['max_instances']}", True)
    field(embed, "Memory", f"{human_mb(usage['memory_mb'])} / {human_mb(quota['max_memory_mb'])}", True)
    field(embed, "Disk", f"{usage['disk_gb']} / {quota['max_disk_gb']} GiB", True)
    field(embed, "Port forwards", f"{ports} / {quota['max_ports']}", True)
    return embed


def guide_embed(images: Iterable[tuple[str, str]]) -> discord.Embed:
    embed = info(
        "How this works",
        "Every instance is its own container with root SSH access. "
        "Use the panel to open a console for any box you own or that was shared with you.",
    )
    field(embed, "Everyday commands", "\n".join(
        (
            "`/vps list` — pick an instance and open its console",
            "`/vps new` — request a box (only if self-service is enabled)",
            "`/vps password` — rotate the root password",
            "`/vps share` / `/vps unshare` — grant or revoke a friend's access",
            "`/ports add` — publish a container port on the host",
        )
    ))
    field(embed, "Console buttons", "\n".join(
        (
            "**Start / Stop / Restart** — power control",
            "**Login details** — current SSH endpoint and root password",
            "**Ports** — create or remove TCP+UDP forwards",
            "**Rebuild** — wipe the disk and redeploy a fresh image",
        )
    ))
    images_text = "\n".join(f"`{ref}` — {label}" for label, ref in images)
    field(embed, "Available images", images_text)
    return embed


# ---------------------------------------------------------------------- views
class PanelView(discord.ui.View):
    """Persistent entry point, safe to re-register after a restart."""

    def __init__(self, bot: Any | None = None) -> None:
        super().__init__(timeout=None)
        if bot is not None:
            self.bot = bot

    @discord.ui.button(
        label="My instances", emoji="🗂️", style=discord.ButtonStyle.primary,
        custom_id="cloudbox:mine", row=0,
    )
    async def mine(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instances = self.bot.store.list_instances(interaction.user.id)
        shared = self.bot.store.list_shared_instances(interaction.user.id)
        combined = instances + [i for i in shared if i["id"] not in {x["id"] for x in instances}]
        if not combined:
            await interaction.response.send_message(
                embed=info("Nothing here yet", "You do not own or share any instances."),
                ephemeral=True,
            )
            return
        await show_picker(interaction, self.bot, combined)

    @discord.ui.button(
        label="Allocation", emoji="📊", style=discord.ButtonStyle.secondary,
        custom_id="cloudbox:usage", row=0,
    )
    async def allocation(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=usage_embed(self.bot.store, interaction.user.id), ephemeral=True
        )

    @discord.ui.button(
        label="Guide", emoji="📘", style=discord.ButtonStyle.secondary,
        custom_id="cloudbox:guide", row=0,
    )
    async def guide(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=guide_embed(self.bot.settings.images), ephemeral=True
        )


async def show_picker(
    interaction: discord.Interaction, bot: Any, instances: list[dict[str, Any]], *, staff: bool = False
) -> None:
    view = InstancePicker(bot, instances, interaction.user.id, staff=staff)
    embed = info("Choose an instance", "Select a box below to open its console.").set_author(
        name=f"{len(instances)} available"
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class InstancePicker(discord.ui.View):
    def __init__(
        self, bot: Any, instances: list[dict[str, Any]], viewer_id: int, *, staff: bool = False
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.viewer_id = viewer_id
        self.staff = staff
        options = [
            discord.SelectOption(
                label=item["name"],
                value=str(item["id"]),
                description=f"{item['state']} · {item['memory_mb'] // 1024} GiB · {item['image']}"[:100],
                emoji=STATE_MARK.get(item["state"], "❔"),
            )
            for item in instances[:25]
        ]
        select = discord.ui.Select(placeholder="Open an instance…", options=options)
        select.callback = self._chosen
        self.add_item(select)

    async def _chosen(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                embed=fail("Not your menu", "Run `/vps list` to get your own."), ephemeral=True
            )
            return
        instance_id = int(interaction.data["values"][0])
        instance = self.bot.store.get_instance(instance_id)
        if instance is None:
            await interaction.response.send_message(embed=fail("Gone", "That instance no longer exists."), ephemeral=True)
            return
        view = ConsoleView(self.bot, instance_id, self.viewer_id, staff=self.staff)
        await interaction.response.edit_message(
            embed=console_embed(self.bot.store, self.bot.provisioner, instance), view=view
        )


class ConsoleView(discord.ui.View):
    def __init__(self, bot: Any, instance_id: int, viewer_id: int, *, staff: bool = False) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.instance_id = instance_id
        self.viewer_id = viewer_id
        self.staff = staff

        # Shared viewers get power + login + ports, never destructive buttons.
        instance = bot.store.get_instance(instance_id)
        owner_id = int(instance["owner_id"]) if instance else viewer_id
        if not staff and owner_id != viewer_id:
            for item in (self.share, self.rebuild, self.delete):
                self.remove_item(item)

    # ------------------------------------------------------------------ guard
    async def _allowed(self, interaction: discord.Interaction) -> dict[str, Any] | None:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                embed=fail("Wrong console", "This console belongs to another user."), ephemeral=True
            )
            return None
        instance = self.bot.store.get_instance(self.instance_id)
        if instance is None or instance["state"] == "deleted":
            await interaction.response.send_message(
                embed=fail("Instance removed", "It no longer exists."), ephemeral=True
            )
            return None
        if not self.staff and not self.bot.store.is_visible_to(instance, interaction.user.id):
            await interaction.response.send_message(
                embed=fail("No access", "This instance is not yours."), ephemeral=True
            )
            return None
        return instance

    async def _refresh(self, interaction: discord.Interaction) -> None:
        instance = self.bot.store.get_instance(self.instance_id)
        if instance is None:
            return
        try:
            await interaction.edit_original_response(
                embed=console_embed(self.bot.store, self.bot.provisioner, instance), view=self
            )
        except discord.HTTPException:
            pass

    def _audit(self, interaction: discord.Interaction, action: str, instance: dict[str, Any], detail: str = "") -> None:
        self.bot.store.audit(interaction.user.id, action, instance["name"], detail)

    # ---------------------------------------------------------------- buttons
    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def power_on(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        if instance["suspended"] and not self.staff:
            await interaction.response.send_message(embed=fail("Locked", "Staff must unlock this box first."), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await self.bot.provisioner.start(instance)
        except Exception as exc:
            await interaction.followup.send(embed=fail("Start failed", str(exc)), ephemeral=True)
            return
        self._audit(interaction, "power:start", instance)
        await self._refresh(interaction)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.secondary, row=0)
    async def power_off(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        await interaction.response.defer()
        try:
            await self.bot.provisioner.stop(instance)
        except Exception as exc:
            await interaction.followup.send(embed=fail("Stop failed", str(exc)), ephemeral=True)
            return
        self._audit(interaction, "power:stop", instance)
        await self._refresh(interaction)

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def power_cycle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        await interaction.response.defer()
        try:
            await self.bot.provisioner.restart(instance)
        except Exception as exc:
            await interaction.followup.send(embed=fail("Restart failed", str(exc)), ephemeral=True)
            return
        self._audit(interaction, "power:restart", instance)
        await self._refresh(interaction)

    @discord.ui.button(label="Login details", emoji="🔑", style=discord.ButtonStyle.primary, row=1)
    async def login(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        password = self.bot.vault.open(instance["password_enc"]) or "unknown — rotate it"
        tunnel = instance.get("tunnel") or ""
        embed = info("Connection details", f"These are for **{instance['name']}** and are only visible to you.")
        if tunnel:
            host, _, port = tunnel.partition(":")
            field(embed, "Command", f"```ssh root@{host} -p {port}```", False)
            field(embed, "Host", f"`{host}`", True)
            field(embed, "Port", f"`{port}`", True)
            field(embed, "User", "`root`", True)
        else:
            field(embed, "Tunnel", "Down. Use **Restart** or `/vps reconnect` to rebuild it.", False)
        field(embed, "Root password", f"`{password}`", False)
        field(embed, "Hygiene", "Rotate with **New password** if this message may have been seen by anyone else.", False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Ports", emoji="🔌", style=discord.ButtonStyle.primary, row=1)
    async def ports(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        await interaction.response.send_message(
            embed=ports_embed(self.bot.store, self.bot.provisioner, instance),
            view=PortsView(self.bot, instance["id"], self.viewer_id, staff=self.staff),
            ephemeral=True,
        )

    @discord.ui.button(label="New password", emoji="🔐", style=discord.ButtonStyle.secondary, row=1)
    async def rotate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            password = await self.bot.provisioner.reset_password(instance)
        except Exception as exc:
            await interaction.followup.send(embed=fail("Rotation failed", str(exc)), ephemeral=True)
            return
        self._audit(interaction, "password:rotate", instance)
        await interaction.followup.send(
            embed=ok("Password rotated", f"New root password for **{instance['name']}**:\n```{password}```"),
            ephemeral=True,
        )

    @discord.ui.button(label="Share", emoji="🤝", style=discord.ButtonStyle.secondary, row=2)
    async def share(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        await interaction.response.send_modal(ShareModal(self.bot, instance))

    @discord.ui.button(label="Rebuild", emoji="🧱", style=discord.ButtonStyle.danger, row=2)
    async def rebuild(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        await interaction.response.send_modal(
            TypedConfirmModal(
                action="Rebuild",
                instance=instance,
                on_confirmed=lambda: _offer_images(interaction, self.bot, instance),
            )
        )

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._allowed(interaction)
        if instance is None:
            return
        await interaction.response.send_modal(
            TypedConfirmModal(
                action="Delete",
                instance=instance,
                on_confirmed=lambda: _destroy(interaction, self.bot, instance),
            )
        )


async def _destroy(interaction: discord.Interaction, bot: Any, instance: dict[str, Any]) -> str:
    await bot.provisioner.destroy(instance)
    bot.store.audit(interaction.user.id, "instance:delete", instance["name"])
    try:
        await interaction.edit_original_response(
            embed=card("Instance removed", f"`{instance['name']}` is gone.", "muted"), view=None
        )
    except discord.HTTPException:
        pass
    return f"**{instance['name']}** was destroyed."


async def _offer_images(interaction: discord.Interaction, bot: Any, instance: dict[str, Any]) -> str:
    await interaction.followup.send(
        embed=info("Pick a fresh image", f"Rebuilding **{instance['name']}** erases its disk."),
        view=ImageChoice(bot, instance["id"]),
        ephemeral=True,
    )
    return ""


# ---------------------------------------------------------------- port views
def ports_embed(store: Store, provisioner: Provisioner, instance: dict[str, Any]) -> discord.Embed:
    rows = store.list_ports(int(instance["id"]))
    embed = info("Port forwards", f"TCP and UDP are published together for **{instance['name']}**.")
    if not rows:
        field(embed, "Active rules", "None yet. Use **New forward** below.")
        return embed
    host_ip = provisioner.settings.public_ip or "your host"
    field(
        embed,
        "Active rules",
        "\n".join(
            f"`{row['host_port']}` → container `{row['guest_port']}`  ·  `{host_ip}:{row['host_port']}`"
            for row in rows[:12]
        ),
    )
    return embed


class PortsView(discord.ui.View):
    def __init__(self, bot: Any, instance_id: int, viewer_id: int, *, staff: bool = False) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.instance_id = instance_id
        self.viewer_id = viewer_id
        self.staff = staff
        rows = bot.store.list_ports(instance_id)
        if rows:
            select = discord.ui.Select(
                placeholder="Remove a forward…",
                options=[
                    discord.SelectOption(
                        label=f"{row['host_port']} → {row['guest_port']}", value=str(row["id"])
                    )
                    for row in rows[:25]
                ],
            )
            select.callback = self._remove
            self.add_item(select)

    async def _resolve(self, interaction: discord.Interaction) -> dict[str, Any] | None:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(embed=fail("Not yours", "Open your own console."), ephemeral=True)
            return None
        instance = self.bot.store.get_instance(self.instance_id)
        if instance is None:
            await interaction.response.send_message(embed=fail("Gone", "Instance no longer exists."), ephemeral=True)
            return None
        return instance

    async def _remove(self, interaction: discord.Interaction) -> None:
        instance = await self._resolve(interaction)
        if instance is None:
            return
        port = self.bot.store.get_port(int(interaction.data["values"][0]))
        if port is None:
            await interaction.response.send_message(embed=fail("Not found", "That rule is already gone."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.provisioner.remove_port(instance, port)
        await interaction.followup.send(
            embed=ok("Forward removed", f"Host port `{port['host_port']}` is free again."), ephemeral=True
        )

    @discord.ui.button(label="New forward", emoji="➕", style=discord.ButtonStyle.primary)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        instance = await self._resolve(interaction)
        if instance is None:
            return
        quota = self.bot.store.get_quota(int(instance["owner_id"]))
        if self.bot.store.count_ports(int(instance["owner_id"])) >= quota["max_ports"] and not self.staff:
            await interaction.response.send_message(
                embed=fail("Quota reached", f"This account allows {quota['max_ports']} forwards."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(PortModal(self.bot, instance))


class PortModal(discord.ui.Modal, title="Publish a container port"):
    guest = discord.ui.TextInput(label="Container port", placeholder="8080", max_length=5)

    def __init__(self, bot: Any, instance: dict[str, Any]) -> None:
        super().__init__()
        self.bot = bot
        self.instance = instance

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            port = int(self.guest.value)
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            await interaction.response.send_message(embed=fail("Bad port", "Use a number from 1 to 65535."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self.bot.provisioner.add_port(self.instance, port)
        except Exception as exc:
            await interaction.followup.send(embed=fail("Forward failed", str(exc)), ephemeral=True)
            return
        host_ip = await self.bot.provisioner.public_ip()
        await interaction.followup.send(
            embed=ok(
                "Forward created",
                f"`{host_ip}:{row['host_port']}` → `{self.instance['name']}:{port}` (TCP + UDP)",
            ),
            ephemeral=True,
        )


# --------------------------------------------------------------------- modals
class TypedConfirmModal(discord.ui.Modal):
    typed = discord.ui.TextInput(
        label="Type the instance name to confirm",
        placeholder="u1234567890-abcd",
        max_length=64,
    )

    def __init__(self, action: str, instance: dict[str, Any], on_confirmed: Any) -> None:
        super().__init__(title=f"{action} {instance['name']}"[:45])
        self.action = action
        self.instance = instance
        self.on_confirmed = on_confirmed

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (self.typed.value or "").strip() != self.instance["name"]:
            await interaction.response.send_message(
                embed=fail("Not confirmed", "The name did not match, so nothing happened."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.on_confirmed()
        except Exception as exc:
            await interaction.followup.send(embed=fail(f"{self.action} failed", str(exc)), ephemeral=True)
            return
        if message:
            await interaction.followup.send(embed=ok(self.action, message), ephemeral=True)


class ShareModal(discord.ui.Modal, title="Share this instance"):
    target = discord.ui.TextInput(label="Discord user ID", placeholder="123456789012345678", max_length=20)

    def __init__(self, bot: Any, instance: dict[str, Any]) -> None:
        super().__init__()
        self.bot = bot
        self.instance = instance

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (self.target.value or "").strip()
        if not raw.isdigit():
            await interaction.response.send_message(embed=fail("Bad ID", "Paste a numeric Discord user ID."), ephemeral=True)
            return
        user_id = int(raw)
        if user_id == int(self.instance["owner_id"]):
            await interaction.response.send_message(embed=fail("That is you", "You already own it."), ephemeral=True)
            return
        self.bot.store.add_share(int(self.instance["id"]), user_id)
        self.bot.store.audit(interaction.user.id, "share:add", self.instance["name"], str(user_id))
        await interaction.response.send_message(
            embed=ok("Shared", f"<@{user_id}> can now open this console with `/vps list`."), ephemeral=True
        )


class ImageChoice(discord.ui.View):
    def __init__(self, bot: Any, instance_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.instance_id = instance_id
        options = [
            discord.SelectOption(label=label, value=ref) for label, ref in bot.settings.images[:25]
        ]
        select = discord.ui.Select(placeholder="Choose the new image…", options=options)
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        instance = self.bot.store.get_instance(self.instance_id)
        if instance is None:
            await interaction.response.send_message(embed=fail("Gone", "Instance no longer exists."), ephemeral=True)
            return
        image = interaction.data["values"][0]
        await interaction.response.edit_message(
            embed=info("Rebuilding", f"Deploying `{image}` — this can take a few minutes."), view=None
        )

        async def progress(text: str) -> None:
            try:
                await interaction.edit_original_response(embed=info("Rebuilding", text))
            except discord.HTTPException:
                pass

        try:
            await self.bot.provisioner.rebuild(instance, image, progress)
        except Exception as exc:
            await interaction.edit_original_response(embed=fail("Rebuild failed", str(exc)))
            return

        self.bot.store.audit(interaction.user.id, "instance:rebuild", instance["name"], image)
        await interaction.edit_original_response(
            embed=ok(
                "Rebuild complete",
                f"**{instance['name']}** is running `{image}` with a new root password.\n"
                "Open **Login details** for the new credentials.",
            )
        )
