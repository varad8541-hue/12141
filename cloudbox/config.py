"""Environment driven configuration for CloudBox."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    return _str(key, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _ids(key: str) -> frozenset[int]:
    found: set[int] = set()
    for chunk in _str(key).replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            found.add(int(chunk))
    return frozenset(found)


def _images(raw: str, fallback: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for part in raw.split(","):
        if "=" not in part:
            continue
        label, ref = part.split("=", 1)
        label, ref = label.strip(), ref.strip()
        if label and ref:
            parsed.append((label, ref))
    return tuple(parsed) or fallback


def _port_range(raw: str) -> tuple[int, int]:
    try:
        low, high = (int(piece) for piece in raw.split("-", 1))
    except ValueError:
        return (20000, 40000)
    if not (1024 < low < high < 65536):
        return (20000, 40000)
    return (low, high)


DEFAULT_IMAGES: tuple[tuple[str, str], ...] = (
    ("Ubuntu 24.04 LTS", "images:ubuntu/24.04"),
    ("Ubuntu 22.04 LTS", "images:ubuntu/22.04"),
    ("Debian 12 Bookworm", "images:debian/12"),
    ("Debian 13 Trixie", "images:debian/13"),
    ("AlmaLinux 9", "images:almalinux/9"),
)


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    brand: str
    guild_id: int | None
    owners: frozenset[int]
    staff_role_id: int | None
    member_role_id: int | None
    data_dir: Path
    storage_pool: str
    default_image: str
    images: tuple[tuple[str, str], ...]
    public_ip: str
    default_ttl_hours: int
    port_range: tuple[int, int]
    allow_privileged: bool
    self_service: bool
    pinggy_token: str
    pinggy_host: str
    audit_channel_id: int | None
    max_instances_staff: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cloudbox.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"


def load_settings() -> Settings:
    data_dir = Path(_str("CLOUDBOX_DATA") or (PACKAGE_ROOT / "data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    guild_raw = _str("GUILD_ID")
    guild_id = int(guild_raw) if guild_raw.isdigit() else None

    staff_raw = _str("STAFF_ROLE_ID")
    member_raw = _str("MEMBER_ROLE_ID")
    audit_raw = _str("AUDIT_CHANNEL_ID")

    return Settings(
        token=_str("DISCORD_TOKEN"),
        brand=_str("BRAND", "CloudBox"),
        guild_id=guild_id,
        owners=_ids("OWNER_IDS") or _ids("MAIN_ADMIN_ID"),
        staff_role_id=int(staff_raw) if staff_raw.isdigit() else None,
        member_role_id=int(member_raw) if member_raw.isdigit() else None,
        data_dir=data_dir,
        storage_pool=_str("STORAGE_POOL", "default"),
        default_image=_str("DEFAULT_IMAGE", DEFAULT_IMAGES[0][1]),
        images=_images(_str("IMAGES"), DEFAULT_IMAGES),
        public_ip=_str("PUBLIC_IP"),
        default_ttl_hours=_int("DEFAULT_TTL_HOURS", 0),
        port_range=_port_range(_str("PORT_RANGE", "20000-40000")),
        allow_privileged=_bool("ALLOW_PRIVILEGED", False),
        self_service=_bool("SELF_SERVICE", False),
        pinggy_token=_str("PINGGY_TOKEN"),
        pinggy_host=_str("PINGGY_HOST", "a.pinggy.io" if _str("PINGGY_TOKEN") else "free.pinggy.io"),
        audit_channel_id=int(audit_raw) if audit_raw.isdigit() else None,
        max_instances_staff=_int("MAX_INSTANCES_STAFF", 25),
    )
