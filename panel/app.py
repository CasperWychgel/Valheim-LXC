#!/usr/bin/env python3
"""Local Valheim administration panel.

The web process is intentionally unprivileged. Privileged service operations go
through the root-owned /usr/local/sbin/valheimctl allow-list.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import psutil
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename


PANEL_CONFIG = Path(os.environ.get("VALHEIM_PANEL_CONFIG", "/etc/valheim/panel.json"))
SERVER_CONFIG = Path(os.environ.get("VALHEIM_SERVER_CONFIG", "/etc/valheim/server.env"))
VALHEIM_HOME = Path(os.environ.get("VALHEIM_HOME", "/opt/valheim"))
DATA_DIR = Path(os.environ.get("VALHEIM_DATA_DIR", str(VALHEIM_HOME / "data")))
WORLD_DIR = Path(os.environ.get("VALHEIM_WORLD_DIR", str(DATA_DIR / "worlds_local")))
BACKUP_DIR = Path(os.environ.get("VALHEIM_BACKUP_DIR", str(VALHEIM_HOME / "backups")))
VALHEIM_LOG = Path(os.environ.get("VALHEIM_LOG", str(VALHEIM_HOME / "logs" / "valheim.log")))
PLAYER_DB = Path(
    os.environ.get("VALHEIM_PLAYER_DB", "/var/lib/valheim-panel/players.sqlite3")
)
MANIFEST = Path(
    os.environ.get(
        "VALHEIM_MANIFEST",
        str(VALHEIM_HOME / "server" / "steamapps" / "appmanifest_896660.acf"),
    )
)
CTL = os.environ.get("VALHEIM_CTL", "/usr/local/sbin/valheimctl")
PANEL_SOURCE_DIR = Path(__file__).resolve().parent

ACCESS_FILES = {
    "admins": "adminlist.txt",
    "bans": "bannedlist.txt",
    "allowlist": "permittedlist.txt",
}
WORLD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
BACKUP_NAME_RE = re.compile(r"^valheim-[0-9]{8}T[0-9]{6}Z\.tar\.gz$")
WORLD_AUTO_BACKUP_RE = re.compile(r"^.+_backup_auto-[0-9]{8}-[0-9]{6}$")
WORLD_GENERATION_RE = re.compile(r"^_main\.(?P<generation>[0-9]+)\.(?P<extension>db2|fwl2|chunks|ok)$")
PLATFORM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{1,127}$")
LOG_TIMESTAMP_RE = re.compile(r"^(?P<timestamp>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}):\s*(?P<message>.*)$")
HANDSHAKE_RE = re.compile(r"Got handshake from client\s+(?P<identifier>[A-Za-z0-9_:-]+)")
CHARACTER_RE = re.compile(
    r"Got character ZDOID from\s+(?P<name>.+?)\s*:\s*(?P<zdo>-?[0-9]+:-?[0-9]+)"
)
CLOSING_SOCKET_RE = re.compile(r"Closing socket\s+(?P<identifier>[A-Za-z0-9_:-]+)")
MAX_PLAYERS = 10
PLAYER_PARSER_VERSION = "2"
MAX_ARCHIVE_MEMBERS = 200_000
MAX_ARCHIVE_EXPANDED_BYTES = 8 * 1024 * 1024 * 1024

DEFAULT_SETTINGS = {
    "SERVER_NAME": "Valheim Dedicated Server",
    "WORLD_NAME": "Dedicated",
    "SERVER_PASSWORD": "Valheim123",
    "GAME_PORT": "2456",
    "PUBLIC": "0",
    "CROSSPLAY": "0",
    "SAVE_INTERVAL": "1800",
    "BACKUPS": "4",
    "BACKUP_SHORT": "7200",
    "BACKUP_LONG": "43200",
    "PRESET": "",
    "COMBAT": "",
    "DEATH_PENALTY": "",
    "RESOURCES": "",
    "RAIDS": "",
    "PORTALS": "",
    "NO_BUILD_COST": "0",
    "PLAYER_EVENTS": "0",
    "PASSIVE_MOBS": "0",
    "NO_MAP": "0",
}

PRESET_CHOICES = ["", "Normal", "Casual", "Easy", "Hard", "Hardcore", "Immersive", "Hammer"]
MODIFIER_CHOICES = {
    "COMBAT": ["", "veryeasy", "easy", "hard", "veryhard"],
    "DEATH_PENALTY": ["", "casual", "veryeasy", "easy", "hard", "hardcore"],
    "RESOURCES": ["", "muchless", "less", "more", "muchmore", "most"],
    "RAIDS": ["", "none", "muchless", "less", "more", "muchmore"],
    "PORTALS": ["", "casual", "hard", "veryhard"],
}


def read_panel_config() -> dict[str, Any]:
    config = json.loads(PANEL_CONFIG.read_text(encoding="utf-8"))
    config.setdefault("show_player_names_on_login", True)
    return config


def password_hash(password: str, *, iterations: int = 390_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_text),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def atomic_text_write(path: Path, content: str, mode: int = 0o660) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_environment_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        try:
            parts = shlex.split(raw_value, posix=True)
            result[key] = parts[0] if parts else ""
        except ValueError:
            result[key] = raw_value.strip().strip('"')
    return result


def quote_environment(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def read_settings() -> dict[str, str]:
    settings = DEFAULT_SETTINGS.copy()
    settings.update(parse_environment_file(SERVER_CONFIG))
    return settings


def write_settings(settings: dict[str, str]) -> None:
    content = "".join(f"{key}={quote_environment(settings[key])}\n" for key in DEFAULT_SETTINGS)
    atomic_text_write(SERVER_CONFIG, content)


def validate_settings(form: Any) -> dict[str, str]:
    settings = read_settings()
    name = form.get("SERVER_NAME", "").strip()
    world = form.get("WORLD_NAME", "").strip()
    password = form.get("SERVER_PASSWORD", "")
    if not password:
        password = settings["SERVER_PASSWORD"]
    if not 1 <= len(name) <= 64 or any(ord(char) < 32 for char in name):
        raise ValueError("Server name must contain 1 to 64 printable characters.")
    if not WORLD_NAME_RE.fullmatch(world):
        raise ValueError("World name may contain letters, numbers, spaces, dots, underscores, and hyphens.")
    if world != settings["WORLD_NAME"]:
        raise ValueError("Use the Worlds page to change the active world safely.")
    if not 5 <= len(password) <= 64 or any(ord(char) < 32 for char in password):
        raise ValueError("Game password must contain 5 to 64 printable characters.")

    def integer(key: str, minimum: int, maximum: int) -> str:
        raw = form.get(key, "").strip()
        try:
            number = int(raw)
        except ValueError as error:
            raise ValueError(f"{key.replace('_', ' ').title()} must be a number.") from error
        if not minimum <= number <= maximum:
            raise ValueError(f"{key.replace('_', ' ').title()} must be between {minimum} and {maximum}.")
        return str(number)

    settings.update(
        {
            "SERVER_NAME": name,
            "WORLD_NAME": world,
            "SERVER_PASSWORD": password,
            "GAME_PORT": integer("GAME_PORT", 1024, 65533),
            "PUBLIC": "1" if form.get("PUBLIC") == "1" else "0",
            "CROSSPLAY": "1" if form.get("CROSSPLAY") == "1" else "0",
            "SAVE_INTERVAL": integer("SAVE_INTERVAL", 60, 86_400),
            "BACKUPS": integer("BACKUPS", 1, 50),
            "BACKUP_SHORT": integer("BACKUP_SHORT", 300, 604_800),
            "BACKUP_LONG": integer("BACKUP_LONG", 300, 2_592_000),
        }
    )
    preset = form.get("PRESET", "")
    if preset not in PRESET_CHOICES:
        raise ValueError("Unknown world preset.")
    settings["PRESET"] = preset
    for key, choices in MODIFIER_CHOICES.items():
        value = form.get(key, "")
        if value not in choices:
            raise ValueError(f"Unknown {key.lower().replace('_', ' ')} modifier.")
        settings[key] = value
    for key in ("NO_BUILD_COST", "PLAYER_EVENTS", "PASSIVE_MOBS", "NO_MAP"):
        settings[key] = "1" if form.get(key) == "1" else "0"
    return settings


def run(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def control(action: str, *arguments: str, timeout: int = 900) -> str:
    if action not in {"start", "stop", "restart", "update", "backup", "restore", "logs"}:
        raise ValueError("Unsupported control action.")
    completed = run(["sudo", "-n", CTL, action, *arguments], timeout=timeout)
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or f"{action.title()} failed.")
    return output


def service_active() -> bool:
    return run(["systemctl", "is-active", "--quiet", "valheim.service"]).returncode == 0


def service_since() -> str:
    completed = run(
        ["systemctl", "show", "valheim.service", "--property=ActiveEnterTimestamp", "--value"]
    )
    return completed.stdout.strip() or "—"


def current_build() -> str:
    if not MANIFEST.exists():
        return "—"
    match = re.search(r'"buildid"\s+"([0-9]+)"', MANIFEST.read_text(encoding="utf-8", errors="ignore"))
    return match.group(1) if match else "—"


def local_address() -> str:
    completed = run(["hostname", "-I"])
    addresses = completed.stdout.split()
    return addresses[0] if addresses else request.host.split(":", 1)[0]


def status_payload() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(VALHEIM_HOME if VALHEIM_HOME.exists() else Path("/")))
    settings = read_settings()
    players = list_players()
    return {
        "active": service_active(),
        "state": "Online" if service_active() else "Offline",
        "cpu": round(psutil.cpu_percent(interval=0.1), 1),
        "memory": round(memory.percent, 1),
        "memory_used": human_bytes(memory.used),
        "memory_total": human_bytes(memory.total),
        "disk": round(disk.percent, 1),
        "disk_free": human_bytes(disk.free),
        "build": current_build(),
        "address": local_address(),
        "game_port": settings["GAME_PORT"],
        "world": settings["WORLD_NAME"],
        "player_count": sum(1 for player in players if player["online"]),
        "max_players": MAX_PLAYERS,
    }


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def relative_time(timestamp: float) -> str:
    delta = max(0, int(time.time() - timestamp))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86_400:
        return f"{delta // 3600} h ago"
    return f"{delta // 86_400} d ago"


def player_database() -> sqlite3.Connection:
    PLAYER_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(PLAYER_DB, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            platform_id TEXT PRIMARY KEY,
            raw_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            connected_at REAL,
            disconnected_at REAL,
            online INTEGER NOT NULL DEFAULT 0,
            connection_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS pending_connections (
            raw_id TEXT PRIMARY KEY,
            connected_at REAL NOT NULL,
            counted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tracker_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    try:
        os.chmod(PLAYER_DB, 0o640)
    except OSError:
        pass
    return connection


def tracker_meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM tracker_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_tracker_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO tracker_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def service_instance_token() -> str:
    if not service_active():
        return "offline"
    completed = run(
        ["systemctl", "show", "valheim.service", "--property=InvocationID", "--value"]
    )
    return completed.stdout.strip() or "active"


def event_timestamp(raw_line: str) -> tuple[float, str]:
    match = LOG_TIMESTAMP_RE.match(raw_line.strip())
    if not match:
        return time.time(), raw_line.strip()
    try:
        parsed = datetime.strptime(match.group("timestamp"), "%m/%d/%Y %H:%M:%S")
        return parsed.timestamp(), match.group("message")
    except ValueError:
        return time.time(), match.group("message")


def platform_user_id(raw_identifier: str) -> str:
    identifier = raw_identifier.strip()
    if identifier.isdigit():
        return f"Steam_{identifier}"
    return identifier[:128]


def safe_player_name(raw_name: str) -> str:
    return "".join(character for character in raw_name.strip() if ord(character) >= 32)[:64]


def mark_players_offline(connection: sqlite3.Connection, timestamp: float) -> None:
    connection.execute(
        "UPDATE players SET online = 0, last_seen = ?, disconnected_at = ? WHERE online = 1",
        (timestamp, timestamp),
    )
    connection.execute("DELETE FROM pending_connections")


def parse_player_log_line(connection: sqlite3.Connection, raw_line: str) -> None:
    timestamp, message = event_timestamp(raw_line)
    handshake = HANDSHAKE_RE.search(message)
    if handshake:
        raw_identifier = handshake.group("identifier")
        pending = connection.execute(
            "SELECT 1 FROM pending_connections WHERE raw_id = ?", (raw_identifier,)
        ).fetchone()
        if pending:
            return
        known = connection.execute(
            "SELECT platform_id FROM players WHERE raw_id = ?", (raw_identifier,)
        ).fetchone()
        counted = 0
        if known:
            connection.execute(
                "UPDATE players SET online = 1, connected_at = ?, disconnected_at = NULL, "
                "last_seen = ?, connection_count = connection_count + 1 WHERE raw_id = ?",
                (timestamp, timestamp, raw_identifier),
            )
            counted = 1
        connection.execute(
            "INSERT OR REPLACE INTO pending_connections (raw_id, connected_at, counted) "
            "VALUES (?, ?, ?)",
            (raw_identifier, timestamp, counted),
        )
        return

    character = CHARACTER_RE.search(message)
    if character:
        name = safe_player_name(character.group("name"))
        if not name:
            return
        pending = connection.execute(
            "SELECT raw_id, connected_at, counted FROM pending_connections "
            "ORDER BY connected_at ASC LIMIT 1"
        ).fetchone()
        if pending:
            raw_identifier = str(pending["raw_id"])
            platform_id = platform_user_id(raw_identifier)
            existing = connection.execute(
                "SELECT 1 FROM players WHERE platform_id = ?", (platform_id,)
            ).fetchone()
            if existing:
                increment = 0 if int(pending["counted"]) else 1
                connection.execute(
                    "UPDATE players SET raw_id = ?, name = ?, online = 1, connected_at = ?, "
                    "disconnected_at = NULL, last_seen = ?, "
                    "connection_count = connection_count + ? WHERE platform_id = ?",
                    (
                        raw_identifier,
                        name,
                        float(pending["connected_at"]),
                        timestamp,
                        increment,
                        platform_id,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO players (platform_id, raw_id, name, first_seen, last_seen, "
                    "connected_at, disconnected_at, online, connection_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, 1, 1)",
                    (
                        platform_id,
                        raw_identifier,
                        name,
                        float(pending["connected_at"]),
                        timestamp,
                        float(pending["connected_at"]),
                    ),
                )
            connection.execute(
                "DELETE FROM pending_connections WHERE raw_id = ?", (raw_identifier,)
            )
        else:
            connection.execute(
                "UPDATE players SET last_seen = ? WHERE name = ? AND online = 1",
                (timestamp, name),
            )
        return

    closing = CLOSING_SOCKET_RE.search(message)
    if closing:
        raw_identifier = closing.group("identifier")
        connection.execute(
            "UPDATE players SET online = 0, last_seen = ?, disconnected_at = ? WHERE raw_id = ?",
            (timestamp, timestamp, raw_identifier),
        )
        connection.execute(
            "DELETE FROM pending_connections WHERE raw_id = ?", (raw_identifier,)
        )


def sync_player_history() -> None:
    connection = player_database()
    now = time.time()
    try:
        connection.execute("BEGIN IMMEDIATE")
        parser_version = tracker_meta(connection, "parser_version")
        if parser_version != PLAYER_PARSER_VERSION:
            known_players = connection.execute("SELECT COUNT(*) FROM players").fetchone()[0]
            connection.execute("DELETE FROM pending_connections")
            if known_players == 0:
                # Earlier parser versions rejected signed Valheim ZDOIDs. Re-read
                # the current log when no usable history was created yet.
                set_tracker_meta(connection, "log_offset", "0")
                set_tracker_meta(connection, "log_signature", "")
            set_tracker_meta(connection, "parser_version", PLAYER_PARSER_VERSION)

        instance_token = service_instance_token()
        previous_instance = tracker_meta(connection, "service_instance")
        if previous_instance and previous_instance != instance_token:
            mark_players_offline(connection, now)
        set_tracker_meta(connection, "service_instance", instance_token)

        if instance_token == "offline":
            mark_players_offline(connection, now)
            connection.commit()
            return
        if not VALHEIM_LOG.is_file():
            connection.commit()
            return

        stat = VALHEIM_LOG.stat()
        signature = f"{stat.st_dev}:{stat.st_ino}"
        previous_signature = tracker_meta(connection, "log_signature")
        try:
            offset = int(tracker_meta(connection, "log_offset", "0"))
        except ValueError:
            offset = 0
        if previous_signature and (previous_signature != signature or stat.st_size < offset):
            mark_players_offline(connection, now)
            offset = 0

        with VALHEIM_LOG.open("rb") as handle:
            handle.seek(offset)
            content = handle.read()
        consumed = len(content)
        if content and not content.endswith(b"\n"):
            final_newline = content.rfind(b"\n")
            if final_newline < 0:
                consumed = 0
                content = b""
            else:
                consumed = final_newline + 1
                content = content[:consumed]
        for raw_line in content.decode("utf-8", errors="replace").splitlines():
            parse_player_log_line(connection, raw_line)

        set_tracker_meta(connection, "log_signature", signature)
        set_tracker_meta(connection, "log_offset", str(offset + consumed))
        connection.execute(
            "DELETE FROM pending_connections WHERE connected_at < ?", (now - 600,)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def access_entry_set(name: str) -> set[str]:
    return {
        line.strip()
        for line in read_access(name).splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    }


def list_players() -> list[dict[str, Any]]:
    try:
        sync_player_history()
        connection = player_database()
        try:
            rows = connection.execute(
                "SELECT * FROM players ORDER BY online DESC, last_seen DESC, name COLLATE NOCASE"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError):
        return []

    admins = access_entry_set("admins")
    bans = access_entry_set("bans")
    allowed = access_entry_set("allowlist")
    players: list[dict[str, Any]] = []
    for row in rows:
        platform_id = str(row["platform_id"])
        online = bool(row["online"])
        if platform_id in bans:
            role = "Banned"
        elif platform_id in admins:
            role = "Admin"
        elif platform_id in allowed:
            role = "Allowed"
        else:
            role = "Player"
        last_timestamp = float(row["disconnected_at"] or row["last_seen"])
        players.append(
            {
                "name": str(row["name"]),
                "platform_id": platform_id,
                "online": online,
                "status": "Online" if online else "Offline",
                "connected": relative_time(float(row["connected_at"])) if online else "—",
                "last_connection": "Online" if online else relative_time(last_timestamp),
                "connection_count": int(row["connection_count"]),
                "role": role,
                "banned": platform_id in bans,
            }
        )
    return players


def online_player_count() -> int:
    """Return a strict live count for unattended maintenance decisions."""
    sync_player_history()
    connection = player_database()
    try:
        row = connection.execute(
            "SELECT COUNT(*) AS player_count FROM players WHERE online = 1"
        ).fetchone()
        return int(row["player_count"])
    finally:
        connection.close()


def public_server_status() -> dict[str, Any]:
    settings = read_settings()
    active = service_active()
    online_players = [player for player in list_players() if player["online"]] if active else []
    show_names = bool(read_panel_config().get("show_player_names_on_login", True))
    return {
        "server_name": settings["SERVER_NAME"],
        "active": active,
        "state": "Online" if active else "Offline",
        "player_count": len(online_players),
        "max_players": MAX_PLAYERS,
        "players": [player["name"] for player in online_players] if show_names else [],
        "show_player_names": show_names,
    }


def safe_archive_parts(raw_name: str) -> tuple[str, ...]:
    """Return a normalized relative archive path or reject the member."""
    if not raw_name or "\x00" in raw_name or "\\" in raw_name or len(raw_name) > 1024:
        raise ValueError("The archive contains an unsafe path.")
    path = PurePosixPath(raw_name.rstrip("/"))
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("The archive contains an unsafe path.")
    return parts


def inspect_archive(path: Path, *, allow_zip: bool = True) -> tuple[str, list[dict[str, Any]]]:
    """Inspect regular archive members without extracting or reading their contents."""
    lower_name = path.name.casefold()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    expanded_size = 0

    def add_entry(name: str, size: int, is_directory: bool) -> None:
        nonlocal expanded_size
        parts = safe_archive_parts(name)
        key = "/".join(part.casefold() for part in parts)
        if key in seen:
            raise ValueError("The archive contains duplicate paths.")
        seen.add(key)
        expanded_size += size
        if len(entries) >= MAX_ARCHIVE_MEMBERS or expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError("The archive is too large when extracted.")
        entries.append(
            {
                "name": "/".join(parts),
                "parts": parts,
                "size": size,
                "is_dir": is_directory,
            }
        )

    if lower_name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if not (member.isdir() or member.isfile()):
                    raise ValueError("The archive contains links or unsupported file types.")
                add_entry(member.name, member.size if member.isfile() else 0, member.isdir())
        kind = "tar"
    elif allow_zip and lower_name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                unix_mode = member.external_attr >> 16
                if member.flag_bits & 0x1:
                    raise ValueError("Encrypted archives are not supported.")
                if unix_mode and stat.S_ISLNK(unix_mode):
                    raise ValueError("The archive contains links or unsupported file types.")
                add_entry(member.filename, member.file_size if not member.is_dir() else 0, member.is_dir())
        kind = "zip"
    else:
        raise ValueError("Use a .tar.gz, .tgz, or .zip archive.")

    if not entries:
        raise ValueError("The archive is empty.")
    return kind, entries


def complete_world_generations(file_names: set[str]) -> list[int]:
    generations: dict[int, set[str]] = {}
    for filename in file_names:
        match = WORLD_GENERATION_RE.fullmatch(filename)
        if match:
            generation = int(match.group("generation"))
            generations.setdefault(generation, set()).add(match.group("extension"))
    required = {"db2", "fwl2", "chunks", "ok"}
    return sorted(generation for generation, extensions in generations.items() if required <= extensions)


def validate_world_archive(path: Path) -> dict[str, Any]:
    kind, entries = inspect_archive(path)
    roots = {entry["parts"][0] for entry in entries}
    if len(roots) != 1:
        raise ValueError("A world archive must contain exactly one top-level world folder.")
    world_name = roots.pop()
    if not WORLD_NAME_RE.fullmatch(world_name) or WORLD_AUTO_BACKUP_RE.fullmatch(world_name):
        raise ValueError("The archive has an invalid or reserved world folder name.")
    if any(len(entry["parts"]) == 1 and not entry["is_dir"] for entry in entries):
        raise ValueError("World files must be stored inside the top-level world folder.")

    direct_files = {
        entry["parts"][1]
        for entry in entries
        if not entry["is_dir"] and len(entry["parts"]) == 2
    }
    generations = complete_world_generations(direct_files)
    chunk_count = sum(filename.endswith(".chunk") for filename in direct_files)
    if not generations or chunk_count == 0:
        raise ValueError(
            "The archive does not contain a complete Valheim 1.0 world generation and chunk data."
        )
    return {
        "kind": kind,
        "entries": entries,
        "world_name": world_name,
        "generation": generations[-1],
        "chunk_count": chunk_count,
    }


def validate_backup_archive(path: Path) -> dict[str, Any]:
    kind, entries = inspect_archive(path, allow_zip=False)
    if kind != "tar":
        raise ValueError("Portable server backups must use .tar.gz.")
    allowed_roots = {"worlds_local", *ACCESS_FILES.values()}
    for entry in entries:
        root = entry["parts"][0]
        if root not in allowed_roots:
            raise ValueError("The archive contains files outside the portable backup layout.")
        if root != "worlds_local" and (len(entry["parts"]) != 1 or entry["is_dir"]):
            raise ValueError("The archive contains an invalid access-list path.")

    regular_names = {entry["name"] for entry in entries if not entry["is_dir"]}
    missing_access = set(ACCESS_FILES.values()) - regular_names
    if missing_access:
        raise ValueError("The archive is missing one or more access lists.")

    directory_files: dict[str, set[str]] = {}
    legacy_extensions: dict[str, set[str]] = {}
    for entry in entries:
        parts = entry["parts"]
        if entry["is_dir"] or parts[0] != "worlds_local":
            continue
        if len(parts) == 3:
            directory_files.setdefault(parts[1], set()).add(parts[2])
        elif len(parts) == 2:
            filename = parts[1]
            for extension in (".db", ".fwl"):
                if filename.endswith(extension):
                    legacy_extensions.setdefault(filename[: -len(extension)], set()).add(extension)

    modern_worlds = [
        name
        for name, files in directory_files.items()
        if WORLD_NAME_RE.fullmatch(name)
        and not WORLD_AUTO_BACKUP_RE.fullmatch(name)
        and complete_world_generations(files)
        and any(filename.endswith(".chunk") for filename in files)
    ]
    legacy_worlds = [
        name
        for name, extensions in legacy_extensions.items()
        if WORLD_NAME_RE.fullmatch(name) and {".db", ".fwl"} <= extensions
    ]
    if not modern_worlds and not legacy_worlds:
        raise ValueError("The archive does not contain a complete playable world.")
    return {
        "entries": entries,
        "modern_worlds": sorted(modern_worlds),
        "legacy_worlds": sorted(legacy_worlds),
    }


def extract_checked_archive(path: Path, destination: Path, kind: str) -> None:
    """Extract only previously supported regular files and directories."""
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "tar":
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = safe_archive_parts(member.name)
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("The archive contains an unreadable file.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    elif kind == "zip":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                parts = safe_archive_parts(member.filename)
                target = destination.joinpath(*parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    else:
        raise ValueError("Unsupported archive type.")


def normalize_world_tree(path: Path) -> None:
    os.chmod(path, 0o2770)
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError("World folders may not contain symbolic links.")
        if item.is_dir():
            os.chmod(item, 0o2770)
        elif item.is_file():
            os.chmod(item, 0o660)
        else:
            raise ValueError("The world contains an unsupported file type.")


def directory_world_details(path: Path, active_name: str) -> dict[str, Any]:
    direct_files = {item.name for item in path.iterdir() if item.is_file() and not item.is_symlink()}
    generations = complete_world_generations(direct_files)
    chunk_count = sum(filename.endswith(".chunk") for filename in direct_files)
    size = 0
    updated = path.stat().st_mtime
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            item_stat = item.stat()
            size += item_stat.st_size
            updated = max(updated, item_stat.st_mtime)
    return {
        "name": path.name,
        "kind": "chunked",
        "format": "Valheim 1.0",
        "complete": bool(generations and chunk_count),
        "generation": generations[-1] if generations else None,
        "chunks": chunk_count,
        "size": human_bytes(size),
        "updated": relative_time(updated),
        "mtime": updated,
        "active": path.name.casefold() == active_name.casefold(),
    }


def list_worlds() -> list[dict[str, Any]]:
    if not WORLD_DIR.is_dir():
        return []
    active_name = read_settings()["WORLD_NAME"]
    worlds_by_name: dict[str, dict[str, Any]] = {}

    for item in WORLD_DIR.iterdir():
        if (
            item.is_dir()
            and not item.is_symlink()
            and WORLD_NAME_RE.fullmatch(item.name)
            and not WORLD_AUTO_BACKUP_RE.fullmatch(item.name)
        ):
            worlds_by_name[item.name.casefold()] = directory_world_details(item, active_name)

    legacy: dict[str, dict[str, Any]] = {}
    for item in WORLD_DIR.iterdir():
        if not item.is_file() or item.is_symlink():
            continue
        extension = item.suffix.casefold()
        if extension not in {".db", ".fwl"}:
            continue
        name = item.name[: -len(extension)]
        if not WORLD_NAME_RE.fullmatch(name):
            continue
        item_stat = item.stat()
        record = legacy.setdefault(
            name.casefold(),
            {
                "name": name,
                "kind": "legacy",
                "format": "Legacy",
                "extensions": set(),
                "bytes": 0,
                "mtime": item_stat.st_mtime,
            },
        )
        record["extensions"].add(extension)
        record["bytes"] += item_stat.st_size
        record["mtime"] = max(record["mtime"], item_stat.st_mtime)

    for key, record in legacy.items():
        if key in worlds_by_name:
            continue
        worlds_by_name[key] = {
            "name": record["name"],
            "kind": "legacy",
            "format": "Legacy",
            "complete": {".db", ".fwl"} <= record["extensions"],
            "generation": None,
            "chunks": None,
            "size": human_bytes(record["bytes"]),
            "updated": relative_time(record["mtime"]),
            "mtime": record["mtime"],
            "active": record["name"].casefold() == active_name.casefold(),
        }

    return sorted(
        worlds_by_name.values(),
        key=lambda world: (not world["active"], -world["mtime"], world["name"].casefold()),
    )


def find_world(name: str) -> dict[str, Any] | None:
    if not WORLD_NAME_RE.fullmatch(name):
        return None
    return next((world for world in list_worlds() if world["name"].casefold() == name.casefold()), None)


def list_backups() -> list[dict[str, str]]:
    backups: list[dict[str, str]] = []
    if not BACKUP_DIR.exists():
        return backups
    for archive in sorted(BACKUP_DIR.glob("valheim-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True):
        if not BACKUP_NAME_RE.fullmatch(archive.name):
            continue
        backups.append(
            {
                "name": archive.name,
                "size": human_bytes(archive.stat().st_size),
                "updated": relative_time(archive.stat().st_mtime),
            }
        )
    return backups


def read_access(name: str) -> str:
    filename = ACCESS_FILES.get(name)
    if not filename:
        abort(404)
    path = DATA_DIR / filename
    return path.read_text(encoding="utf-8") if path.exists() else ""


def validate_access(content: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in content.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("//"):
            lines.append(line[:256])
            continue
        if not PLATFORM_ID_RE.fullmatch(line):
            raise ValueError(f"Invalid platform user ID: {line[:40]}")
        if line not in seen:
            lines.append(line)
            seen.add(line)
    return "\n".join(lines) + "\n"


def set_player_banned(platform_id: str, banned: bool) -> None:
    if not PLATFORM_ID_RE.fullmatch(platform_id):
        raise ValueError("Invalid platform user ID.")
    lines = read_access("bans").replace("\r\n", "\n").splitlines()
    updated = [line for line in lines if line.strip() != platform_id]
    if banned:
        updated.append(platform_id)
    content = "\n".join(updated).rstrip() + "\n"
    atomic_text_write(DATA_DIR / ACCESS_FILES["bans"], content)


def world_files_exist(name: str) -> bool:
    if not WORLD_DIR.is_dir():
        return False
    expected = {
        f"{name}.db".casefold(),
        f"{name}.fwl".casefold(),
        f"{name}.db.old".casefold(),
        f"{name}.fwl.old".casefold(),
    }
    return any(
        (item.is_dir() and item.name.casefold() == name.casefold())
        or (item.is_file() and item.name.casefold() in expected)
        for item in WORLD_DIR.iterdir()
    )


def create_world_archive(world: dict[str, Any]) -> Path:
    if world["kind"] != "chunked":
        raise ValueError("Only Valheim 1.0 folder worlds are downloaded as archives.")
    source = WORLD_DIR / world["name"]
    if not source.is_dir() or source.is_symlink():
        raise ValueError("The world folder is unavailable.")
    descriptor, temporary_name = tempfile.mkstemp(prefix="valheim-world-", suffix=".tar.gz")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, mode="w:gz") as archive:
            paths = [source, *sorted(source.rglob("*"), key=lambda item: item.as_posix())]
            for path in paths:
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise ValueError("The world contains an unsupported file type.")
                archive_name = str(PurePosixPath(source.name, *path.relative_to(source).parts))
                info = archive.gettarinfo(str(path), arcname=archive_name)
                info.uid = 0
                info.gid = 0
                info.uname = "valheim"
                info.gname = "valheim-admin"
                info.mode = 0o750 if path.is_dir() else 0o640
                if path.is_dir():
                    archive.addfile(info)
                else:
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def ensure_world_change_is_safe() -> None:
    try:
        player_count = online_player_count()
    except Exception as error:
        raise RuntimeError(
            "World changes are blocked because player activity could not be verified."
        ) from error
    if player_count:
        raise RuntimeError(
            f"World changes are blocked while {player_count} player(s) are online."
        )


def activate_world(name: str) -> str:
    ensure_world_change_is_safe()
    previous_settings = read_settings()
    if previous_settings["WORLD_NAME"].casefold() == name.casefold():
        raise ValueError("This world is already active.")

    backup_output = control("backup")
    updated_settings = previous_settings.copy()
    updated_settings["WORLD_NAME"] = name
    write_settings(updated_settings)
    try:
        restart_output = control("restart")
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        write_settings(previous_settings)
        try:
            control("restart")
            recovery = " The previous world configuration was restored and restarted."
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            recovery = " The previous configuration was restored, but its restart also failed."
        raise RuntimeError(f"The new world could not be activated.{recovery}") from error
    return " ".join(part for part in (backup_output, restart_output) if part)


def create_app() -> Flask:
    config = read_panel_config()
    application = Flask(
        __name__,
        template_folder=str(PANEL_SOURCE_DIR / "templates"),
        static_folder=str(PANEL_SOURCE_DIR / "static"),
    )
    application.secret_key = config["session_secret"]
    application.config.update(
        MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
    )
    login_attempts: dict[str, list[float]] = {}

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.full_path))
            return view(*args, **kwargs)

        return wrapped

    @application.before_request
    def enforce_csrf() -> None:
        if request.method == "POST" and request.endpoint != "login":
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
            if not expected or not hmac.compare_digest(expected, supplied):
                abort(400, "The form expired. Reload the page and try again.")

    @application.context_processor
    def template_context() -> dict[str, Any]:
        return {
            "csrf_token": session.get("csrf_token", ""),
            "current_year": datetime.now(timezone.utc).year,
            "panel_port": os.environ.get("VALHEIM_PANEL_PORT", "2460"),
        }

    @application.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        )
        return response

    @application.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            client = request.remote_addr or "unknown"
            now = time.time()
            attempts = [value for value in login_attempts.get(client, []) if now - value < 300]
            if len(attempts) >= 8:
                return (
                    render_template(
                        "login.html",
                        error="Too many attempts. Try again in five minutes.",
                        public_status=public_server_status(),
                    ),
                    429,
                )
            panel_config = read_panel_config()
            username_ok = hmac.compare_digest(request.form.get("username", ""), panel_config["username"])
            password_ok = password_matches(request.form.get("password", ""), panel_config["password_hash"])
            if username_ok and password_ok:
                login_attempts.pop(client, None)
                session.clear()
                session["authenticated"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                return redirect(url_for("dashboard"))
            attempts.append(now)
            login_attempts[client] = attempts
            return (
                render_template(
                    "login.html",
                    error="Incorrect username or password.",
                    public_status=public_server_status(),
                ),
                401,
            )
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        return render_template(
            "login.html", error=None, public_status=public_server_status()
        )

    @application.get("/api/public-status")
    def api_public_status():
        response = jsonify(public_server_status())
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @application.get("/")
    @login_required
    def dashboard():
        tab = request.args.get("tab", "overview")
        if tab not in {
            "overview",
            "players",
            "settings",
            "access",
            "worlds",
            "backups",
            "logs",
            "security",
        }:
            tab = "overview"
        logs = ""
        if tab == "logs":
            try:
                logs = control("logs", timeout=20)
            except RuntimeError as error:
                logs = str(error)
        return render_template(
            "dashboard.html",
            tab=tab,
            status=status_payload(),
            settings=read_settings(),
            players=list_players() if tab == "players" else [],
            panel_config=read_panel_config(),
            preset_choices=PRESET_CHOICES,
            modifier_choices=MODIFIER_CHOICES,
            access={key: read_access(key) for key in ACCESS_FILES} if tab == "access" else {},
            worlds=list_worlds() if tab == "worlds" else [],
            backups=list_backups() if tab == "backups" else [],
            logs=logs,
            service_since=service_since(),
        )

    @application.get("/api/status")
    @login_required
    def api_status():
        return jsonify(status_payload())

    @application.get("/api/players")
    @login_required
    def api_players():
        players = list_players()
        return jsonify(
            {
                "players": players,
                "player_count": sum(1 for player in players if player["online"]),
                "max_players": MAX_PLAYERS,
            }
        )

    @application.post("/player/<platform_id>/ban")
    @login_required
    def player_ban(platform_id: str):
        action_name = request.form.get("action", "")
        if action_name not in {"ban", "unban"}:
            abort(400, "Unknown player action.")
        try:
            set_player_banned(platform_id, action_name == "ban")
            flash(
                f"{platform_id} {'banned' if action_name == 'ban' else 'unbanned'}.",
                "success",
            )
        except (ValueError, OSError) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="players"))

    @application.post("/action/<action>")
    @login_required
    def action(action: str):
        if action not in {"start", "stop", "restart", "update", "backup"}:
            abort(404)
        try:
            output = control(action)
            flash(output or f"{action.title()} completed.", "success")
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="backups" if action == "backup" else "overview"))

    @application.post("/settings")
    @login_required
    def settings_save():
        try:
            settings = validate_settings(request.form)
            write_settings(settings)
            flash("Settings saved. Restart the server to apply them.", "success")
        except (ValueError, OSError) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="settings"))

    @application.post("/access/<name>")
    @login_required
    def access_save(name: str):
        filename = ACCESS_FILES.get(name)
        if not filename:
            abort(404)
        try:
            content = validate_access(request.form.get("entries", ""))
            atomic_text_write(DATA_DIR / filename, content)
            flash(f"{name.title()} saved.", "success")
        except (ValueError, OSError) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="access"))

    @application.post("/world/select/<name>")
    @login_required
    def world_select(name: str):
        world = find_world(name)
        if not world or not world["complete"]:
            abort(404)
        try:
            activate_world(world["name"])
            if world["kind"] == "legacy":
                flash(
                    f"Legacy world {world['name']} activated. Valheim 1.0 is migrating it to the new folder format.",
                    "success",
                )
            else:
                flash(f"World {world['name']} selected and the server restarted.", "success")
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="worlds"))

    @application.post("/world/create")
    @login_required
    def world_create():
        name = request.form.get("world_name", "").strip()
        try:
            if not WORLD_NAME_RE.fullmatch(name):
                raise ValueError(
                    "World name may contain letters, numbers, spaces, dots, underscores, and hyphens."
                )
            if world_files_exist(name):
                raise ValueError("A world with this name already exists.")
            activate_world(name)
            flash(
                f"World {name} is active. Valheim is generating its new random seed.",
                "success",
            )
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="worlds"))

    @application.post("/world/backup/<name>")
    @login_required
    def world_backup(name: str):
        world = find_world(name)
        if not world:
            abort(404)
        try:
            control("backup")
            flash(
                f"Snapshot created. The archive includes {world['name']} and the complete world library.",
                "success",
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="worlds"))

    @application.post("/world/delete/<name>")
    @login_required
    def world_delete(name: str):
        world = find_world(name)
        if not world:
            abort(404)
        if world["active"]:
            flash("The active world cannot be deleted. Select another world first.", "error")
            return redirect(url_for("dashboard", tab="worlds"))
        try:
            ensure_world_change_is_safe()
            control("backup")
            if world["kind"] == "chunked":
                candidate = (WORLD_DIR / world["name"]).resolve()
                if candidate.parent != WORLD_DIR.resolve() or not candidate.is_dir() or candidate.is_symlink():
                    raise ValueError("The world folder could not be validated.")
                shutil.rmtree(candidate)
            else:
                for suffix in (".db", ".fwl", ".db.old", ".fwl.old"):
                    candidate = WORLD_DIR / f"{world['name']}{suffix}"
                    if candidate.is_file() and not candidate.is_symlink():
                        candidate.unlink()
            flash(f"World {world['name']} deleted after creating a safety snapshot.", "success")
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="worlds"))

    @application.get("/world/download/<name>/<extension>")
    @login_required
    def world_download(name: str, extension: str):
        world = find_world(name)
        if not world or extension not in {"archive", "db", "fwl"}:
            abort(404)
        if extension == "archive":
            if world["kind"] != "chunked":
                abort(404)
            try:
                temporary = create_world_archive(world)
            except (ValueError, OSError, tarfile.TarError):
                abort(500)
            response = send_file(
                temporary,
                as_attachment=True,
                download_name=f"{world['name']}.tar.gz",
            )
            response.call_on_close(lambda: temporary.unlink(missing_ok=True))
            return response
        if world["kind"] != "legacy":
            abort(404)
        path = WORLD_DIR / f"{world['name']}.{extension}"
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @application.post("/world/upload")
    @application.post("/world/upload/legacy")
    @login_required
    def world_upload_legacy():
        database = request.files.get("database")
        descriptor = request.files.get("descriptor")
        if not database or not descriptor:
            flash("Select both a .db and matching .fwl file.", "error")
            return redirect(url_for("dashboard", tab="worlds"))
        database_name = secure_filename(database.filename or "")
        descriptor_name = secure_filename(descriptor.filename or "")
        if not database_name.endswith(".db") or not descriptor_name.endswith(".fwl"):
            flash("World uploads require one .db and one .fwl file.", "error")
            return redirect(url_for("dashboard", tab="worlds"))
        world_name = database_name[:-3]
        if descriptor_name[:-4] != world_name or not WORLD_NAME_RE.fullmatch(world_name):
            flash("The .db and .fwl filenames must use the same valid world name.", "error")
            return redirect(url_for("dashboard", tab="worlds"))
        if world_files_exist(world_name):
            flash(
                "A world with this name already exists. Delete the inactive world first or choose another name.",
                "error",
            )
            return redirect(url_for("dashboard", tab="worlds"))
        WORLD_DIR.mkdir(parents=True, exist_ok=True, mode=0o2770)
        with tempfile.TemporaryDirectory(dir=WORLD_DIR) as temporary:
            staging = Path(temporary)
            database.save(staging / database_name)
            descriptor.save(staging / descriptor_name)
            if not (staging / database_name).stat().st_size or not (staging / descriptor_name).stat().st_size:
                flash("Legacy world files may not be empty.", "error")
                return redirect(url_for("dashboard", tab="worlds"))
            os.replace(staging / database_name, WORLD_DIR / database_name)
            os.replace(staging / descriptor_name, WORLD_DIR / descriptor_name)
            os.chmod(WORLD_DIR / database_name, 0o660)
            os.chmod(WORLD_DIR / descriptor_name, 0o660)
        flash(
            f"Legacy world {world_name} uploaded. Use Migrate & activate to let Valheim 1.0 convert it safely.",
            "success",
        )
        return redirect(url_for("dashboard", tab="worlds"))

    @application.post("/world/upload/archive")
    @login_required
    def world_upload_archive():
        upload = request.files.get("world_archive")
        filename = secure_filename(upload.filename or "") if upload else ""
        if not upload or not filename.casefold().endswith((".tar.gz", ".tgz", ".zip")):
            flash("Select a Valheim 1.0 .tar.gz, .tgz, or .zip world archive.", "error")
            return redirect(url_for("dashboard", tab="worlds"))
        WORLD_DIR.mkdir(parents=True, exist_ok=True, mode=0o2770)
        try:
            with tempfile.TemporaryDirectory(dir=WORLD_DIR, prefix=".world-import-") as temporary:
                staging = Path(temporary)
                archive_path = staging / filename
                extracted = staging / "extracted"
                upload.save(archive_path)
                metadata = validate_world_archive(archive_path)
                world_name = metadata["world_name"]
                if world_files_exist(world_name):
                    raise ValueError(
                        "A world with this name already exists. Existing worlds are never overwritten."
                    )
                extract_checked_archive(archive_path, extracted, metadata["kind"])
                imported_world = extracted / world_name
                if not imported_world.is_dir():
                    raise ValueError("The archive did not produce the expected world folder.")
                normalize_world_tree(imported_world)
                if world_files_exist(world_name):
                    raise ValueError("A world with this name appeared during the import. Nothing was overwritten.")
                os.replace(imported_world, WORLD_DIR / world_name)
            flash(
                f"Valheim 1.0 world {world_name} imported. Activate it when the server is empty.",
                "success",
            )
        except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="worlds"))

    @application.get("/backup/download/<name>")
    @login_required
    def backup_download(name: str):
        if not BACKUP_NAME_RE.fullmatch(name):
            abort(404)
        path = BACKUP_DIR / name
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=name)

    @application.post("/backup/restore/<name>")
    @login_required
    def backup_restore(name: str):
        if not BACKUP_NAME_RE.fullmatch(name):
            abort(404)
        try:
            archive = BACKUP_DIR / name
            if not archive.is_file():
                abort(404)
            metadata = validate_backup_archive(archive)
            available_worlds = {
                world.casefold()
                for world in (*metadata["modern_worlds"], *metadata["legacy_worlds"])
            }
            active_world = read_settings()["WORLD_NAME"]
            if active_world.casefold() not in available_worlds:
                raise ValueError(
                    f"This backup does not contain the currently selected world {active_world}. "
                    "Import its world separately or restore a matching server backup."
                )
            ensure_world_change_is_safe()
            output = control("restore", name)
            flash(output, "success")
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, tarfile.TarError) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="backups"))

    @application.post("/backup/upload")
    @login_required
    def backup_upload():
        upload = request.files.get("backup_archive")
        filename = secure_filename(upload.filename or "") if upload else ""
        if not upload or not BACKUP_NAME_RE.fullmatch(filename):
            flash("Select an original valheim-YYYYMMDDTHHMMSSZ.tar.gz server backup.", "error")
            return redirect(url_for("dashboard", tab="backups"))
        BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o2770)
        destination = BACKUP_DIR / filename
        if destination.exists():
            flash("A backup with this name already exists. It was not overwritten.", "error")
            return redirect(url_for("dashboard", tab="backups"))
        try:
            with tempfile.TemporaryDirectory(dir=BACKUP_DIR, prefix=".backup-import-") as temporary:
                staging = Path(temporary) / filename
                upload.save(staging)
                metadata = validate_backup_archive(staging)
                if destination.exists():
                    raise ValueError("A backup with this name appeared during the upload.")
                os.replace(staging, destination)
                os.chmod(destination, 0o660)
            world_count = len(metadata["modern_worlds"]) + len(metadata["legacy_worlds"])
            flash(
                f"Server backup uploaded and validated ({world_count} playable world(s)). Review it before restoring.",
                "success",
            )
        except (ValueError, OSError, tarfile.TarError) as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="backups"))

    @application.post("/backup/delete/<name>")
    @login_required
    def backup_delete(name: str):
        if not BACKUP_NAME_RE.fullmatch(name):
            abort(404)
        path = BACKUP_DIR / name
        if not path.is_file():
            abort(404)
        path.unlink()
        flash(f"Deleted {name}.", "success")
        return redirect(url_for("dashboard", tab="backups"))

    @application.post("/security/password")
    @login_required
    def security_password():
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirmation = request.form.get("confirm_password", "")
        panel_config = read_panel_config()
        if not password_matches(current, panel_config["password_hash"]):
            flash("Current panel password is incorrect.", "error")
        elif len(new) < 12:
            flash("New panel password must contain at least 12 characters.", "error")
        elif new != confirmation:
            flash("New password and confirmation do not match.", "error")
        else:
            panel_config["password_hash"] = password_hash(new)
            atomic_text_write(PANEL_CONFIG, json.dumps(panel_config, indent=2) + "\n", mode=0o600)
            flash("Panel password changed.", "success")
        return redirect(url_for("dashboard", tab="security"))

    @application.post("/security/privacy")
    @login_required
    def security_privacy():
        panel_config = read_panel_config()
        panel_config["show_player_names_on_login"] = (
            request.form.get("show_player_names_on_login") == "1"
        )
        try:
            atomic_text_write(
                PANEL_CONFIG, json.dumps(panel_config, indent=2) + "\n", mode=0o600
            )
            flash("Sign-in page privacy setting saved.", "success")
        except OSError as error:
            flash(str(error), "error")
        return redirect(url_for("dashboard", tab="security"))

    return application


def set_password(username: str, password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise SystemExit("Username contains unsupported characters.")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters.")
    config = read_panel_config()
    config["username"] = username
    config["password_hash"] = password_hash(password)
    atomic_text_write(PANEL_CONFIG, json.dumps(config, indent=2) + "\n", mode=0o600)
    print(f"Panel login updated for {username}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Valheim admin panel")
    parser.add_argument("--set-password", nargs=2, metavar=("USERNAME", "PASSWORD"))
    parser.add_argument("--online-player-count", action="store_true")
    parser.add_argument("--validate-backup-archive", type=Path, metavar="ARCHIVE")
    args = parser.parse_args()
    if args.set_password:
        set_password(*args.set_password)
        return
    if args.online_player_count:
        try:
            print(online_player_count())
        except Exception as error:
            raise SystemExit(f"Could not determine the online player count: {error}") from error
        return
    if args.validate_backup_archive:
        try:
            metadata = validate_backup_archive(args.validate_backup_archive)
        except (ValueError, OSError, tarfile.TarError) as error:
            raise SystemExit(f"Invalid portable backup: {error}") from error
        world_count = len(metadata["modern_worlds"]) + len(metadata["legacy_worlds"])
        print(f"Validated portable backup with {world_count} playable world(s).")
        return
    parser.error("Start the panel with Waitress or use a supported command option.")


if __name__ == "__main__":
    main()
else:
    app = create_app()
