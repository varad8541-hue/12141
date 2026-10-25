"""SQLite persistence for CloudBox.

A single connection guarded by a lock is plenty for a control-plane bot: every
statement here touches a handful of rows and runs far faster than a Discord
round trip. Long running work (LXC, ssh) never holds the lock.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    region      TEXT    NOT NULL DEFAULT '',
    kind        TEXT    NOT NULL DEFAULT 'local',
    endpoint    TEXT    NOT NULL DEFAULT '',
    secret      TEXT    NOT NULL DEFAULT '',
    capacity    INTEGER NOT NULL DEFAULT 10,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    node_id     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL UNIQUE,
    state       TEXT    NOT NULL DEFAULT 'provisioning',
    image       TEXT    NOT NULL,
    memory_mb   INTEGER NOT NULL,
    vcpu        INTEGER NOT NULL,
    disk_gb     INTEGER NOT NULL,
    docker      INTEGER NOT NULL DEFAULT 0,
    privileged  INTEGER NOT NULL DEFAULT 0,
    suspended   INTEGER NOT NULL DEFAULT 0,
    password_enc TEXT   NOT NULL DEFAULT '',
    tunnel      TEXT    NOT NULL DEFAULT '',
    expires_at  TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_owner ON instances(owner_id);

CREATE TABLE IF NOT EXISTS shares (
    instance_id INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (instance_id, user_id)
);

CREATE TABLE IF NOT EXISTS ports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    host_port   INTEGER NOT NULL UNIQUE,
    guest_port  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ports_instance ON ports(instance_id);

CREATE TABLE IF NOT EXISTS quotas (
    user_id         INTEGER PRIMARY KEY,
    max_instances   INTEGER NOT NULL,
    max_memory_mb   INTEGER NOT NULL,
    max_disk_gb     INTEGER NOT NULL,
    max_ports       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    subject     TEXT    NOT NULL DEFAULT '',
    detail      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_QUOTA = (2, 4096, 40, 4)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime | None = None) -> str:
    return (moment or utcnow()).isoformat(timespec="seconds")


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- helpers
    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def _write(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------ nodes
    def add_node(
        self,
        name: str,
        region: str,
        kind: str,
        endpoint: str,
        secret: str,
        capacity: int,
    ) -> int:
        return self._write(
            "INSERT INTO nodes (name, region, kind, endpoint, secret, capacity, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, region, kind, endpoint, secret, capacity, stamp()),
        )

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM nodes WHERE id = ?", (node_id,))

    def node_by_name(self, name: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM nodes WHERE name = ?", (name,))

    def list_nodes(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM nodes"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self._all(sql + " ORDER BY id")

    def delete_node(self, node_id: int) -> None:
        self._write("DELETE FROM nodes WHERE id = ?", (node_id,))

    def set_node_enabled(self, node_id: int, enabled: bool) -> None:
        self._write("UPDATE nodes SET enabled = ? WHERE id = ?", (1 if enabled else 0, node_id))

    def ensure_local_node(self, capacity: int = 50) -> dict[str, Any]:
        node = self._one("SELECT * FROM nodes WHERE kind = 'local' ORDER BY id LIMIT 1")
        if node:
            return node
        self.add_node("local", "on-site", "local", "", "", capacity)
        node = self._one("SELECT * FROM nodes WHERE kind = 'local' ORDER BY id LIMIT 1")
        assert node is not None
        return node

    def node_load(self, node_id: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM instances WHERE node_id = ? AND state != 'deleted'",
            (node_id,),
        )
        return int(row["n"]) if row else 0

    # -------------------------------------------------------------- instances
    def create_instance(
        self,
        owner_id: int,
        node_id: int,
        name: str,
        image: str,
        memory_mb: int,
        vcpu: int,
        disk_gb: int,
        docker: bool,
        privileged: bool,
        expires_at: str | None,
    ) -> int:
        now = stamp()
        return self._write(
            "INSERT INTO instances (owner_id, node_id, name, state, image, memory_mb, vcpu,"
            " disk_gb, docker, privileged, expires_at, created_at, updated_at)"
            " VALUES (?, ?, ?, 'provisioning', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                owner_id,
                node_id,
                name,
                image,
                memory_mb,
                vcpu,
                disk_gb,
                1 if docker else 0,
                1 if privileged else 0,
                expires_at,
                now,
                now,
            ),
        )

    _INSTANCE_FIELDS = {
        "state",
        "image",
        "memory_mb",
        "vcpu",
        "disk_gb",
        "suspended",
        "password_enc",
        "tunnel",
        "expires_at",
        "node_id",
        "updated_at",
    }

    def update_instance(self, instance_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._INSTANCE_FIELDS
        if unknown:
            raise ValueError(f"unknown instance fields: {sorted(unknown)}")
        fields["updated_at"] = stamp()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        params = list(fields.values()) + [instance_id]
        self._write(f"UPDATE instances SET {assignments} WHERE id = ?", params)

    def get_instance(self, instance_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM instances WHERE id = ?", (instance_id,))

    def instance_by_name(self, name: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM instances WHERE name = ?", (name,))

    def list_instances(self, owner_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM instances WHERE owner_id = ? AND state != 'deleted' ORDER BY id",
            (owner_id,),
        )

    def list_all_instances(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM instances"
        if not include_deleted:
            sql += " WHERE state != 'deleted'"
        return self._all(sql + " ORDER BY id")

    def list_shared_instances(self, user_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT i.* FROM instances i JOIN shares s ON s.instance_id = i.id"
            " WHERE s.user_id = ? AND i.state != 'deleted' ORDER BY i.id",
            (user_id,),
        )

    def delete_instance_row(self, instance_id: int) -> None:
        self._write("DELETE FROM instances WHERE id = ?", (instance_id,))

    def is_visible_to(self, instance: dict[str, Any], user_id: int) -> bool:
        if int(instance["owner_id"]) == user_id:
            return True
        row = self._one(
            "SELECT 1 AS ok FROM shares WHERE instance_id = ? AND user_id = ?",
            (instance["id"], user_id),
        )
        return row is not None

    def usage(self, owner_id: int) -> dict[str, int]:
        row = self._one(
            "SELECT COUNT(*) AS count, COALESCE(SUM(memory_mb), 0) AS memory,"
            " COALESCE(SUM(disk_gb), 0) AS disk"
            " FROM instances WHERE owner_id = ? AND state != 'deleted'",
            (owner_id,),
        )
        assert row is not None
        return {
            "count": int(row["count"]),
            "memory_mb": int(row["memory"]),
            "disk_gb": int(row["disk"]),
        }

    def expired_instances(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM instances WHERE state = 'running' AND suspended = 0"
            " AND expires_at IS NOT NULL AND expires_at < ?",
            (stamp(),),
        )

    # ----------------------------------------------------------------- shares
    def add_share(self, instance_id: int, user_id: int) -> None:
        self._write(
            "INSERT OR IGNORE INTO shares (instance_id, user_id, created_at) VALUES (?, ?, ?)",
            (instance_id, user_id, stamp()),
        )

    def remove_share(self, instance_id: int, user_id: int) -> None:
        self._write(
            "DELETE FROM shares WHERE instance_id = ? AND user_id = ?", (instance_id, user_id)
        )

    def list_shares(self, instance_id: int) -> list[int]:
        rows = self._all("SELECT user_id FROM shares WHERE instance_id = ?", (instance_id,))
        return [int(row["user_id"]) for row in rows]

    # ------------------------------------------------------------------ ports
    def add_port(self, instance_id: int, host_port: int, guest_port: int) -> int:
        return self._write(
            "INSERT INTO ports (instance_id, host_port, guest_port, created_at)"
            " VALUES (?, ?, ?, ?)",
            (instance_id, host_port, guest_port, stamp()),
        )

    def get_port(self, port_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM ports WHERE id = ?", (port_id,))

    def list_ports(self, instance_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM ports WHERE instance_id = ? ORDER BY host_port", (instance_id,)
        )

    def count_ports(self, owner_id: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM ports p JOIN instances i ON i.id = p.instance_id"
            " WHERE i.owner_id = ?",
            (owner_id,),
        )
        return int(row["n"]) if row else 0

    def host_port_taken(self, host_port: int) -> bool:
        return self._one("SELECT 1 AS ok FROM ports WHERE host_port = ?", (host_port,)) is not None

    def delete_port(self, port_id: int) -> None:
        self._write("DELETE FROM ports WHERE id = ?", (port_id,))

    # ----------------------------------------------------------------- quotas
    def get_quota(self, user_id: int) -> dict[str, int]:
        row = self._one("SELECT * FROM quotas WHERE user_id = ?", (user_id,))
        if row is None:
            base = DEFAULT_QUOTA
            return {
                "user_id": user_id,
                "max_instances": base[0],
                "max_memory_mb": base[1],
                "max_disk_gb": base[2],
                "max_ports": base[3],
            }
        return {
            "user_id": user_id,
            "max_instances": int(row["max_instances"]),
            "max_memory_mb": int(row["max_memory_mb"]),
            "max_disk_gb": int(row["max_disk_gb"]),
            "max_ports": int(row["max_ports"]),
        }

    def set_quota(
        self, user_id: int, instances: int, memory_mb: int, disk_gb: int, ports: int
    ) -> None:
        self._write(
            "INSERT INTO quotas (user_id, max_instances, max_memory_mb, max_disk_gb, max_ports)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET max_instances = excluded.max_instances,"
            " max_memory_mb = excluded.max_memory_mb, max_disk_gb = excluded.max_disk_gb,"
            " max_ports = excluded.max_ports",
            (user_id, instances, memory_mb, disk_gb, ports),
        )

    # ------------------------------------------------------------------ audit
    def audit(self, actor_id: int, action: str, subject: str = "", detail: str = "") -> None:
        self._write(
            "INSERT INTO audit (actor_id, action, subject, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (actor_id, action, subject, detail, stamp()),
        )

    def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def default_expiry(hours: int) -> str | None:
    if hours <= 0:
        return None
    return stamp(utcnow() + timedelta(hours=hours))
