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
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
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
PLATFORM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{1,127}$")

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


def read_panel_config() -> dict[str, str]:
    return json.loads(PANEL_CONFIG.read_text(encoding="utf-8"))


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


def list_worlds() -> list[dict[str, Any]]:
    worlds: list[dict[str, Any]] = []
    if not WORLD_DIR.exists():
        return worlds
    for database in sorted(WORLD_DIR.glob("*.db"), key=lambda item: item.stat().st_mtime, reverse=True):
        name = database.stem
        world_file = WORLD_DIR / f"{name}.fwl"
        size = database.stat().st_size + (world_file.stat().st_size if world_file.exists() else 0)
        worlds.append(
            {
                "name": name,
                "complete": world_file.exists(),
                "size": human_bytes(size),
                "updated": relative_time(database.stat().st_mtime),
            }
        )
    return worlds


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


def create_app() -> Flask:
    config = read_panel_config()
    application = Flask(
        __name__,
        template_folder=str(PANEL_SOURCE_DIR / "templates"),
        static_folder=str(PANEL_SOURCE_DIR / "static"),
    )
    application.secret_key = config["session_secret"]
    application.config.update(
        MAX_CONTENT_LENGTH=256 * 1024 * 1024,
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
                return render_template("login.html", error="Too many attempts. Try again in five minutes."), 429
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
            return render_template("login.html", error="Incorrect username or password."), 401
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", error=None)

    @application.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @application.get("/")
    @login_required
    def dashboard():
        tab = request.args.get("tab", "overview")
        if tab not in {"overview", "settings", "access", "worlds", "backups", "logs", "security"}:
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
        if not WORLD_NAME_RE.fullmatch(name) or not (WORLD_DIR / f"{name}.db").is_file():
            abort(404)
        settings = read_settings()
        settings["WORLD_NAME"] = name
        write_settings(settings)
        flash(f"World set to {name}. Restart the server to load it.", "success")
        return redirect(url_for("dashboard", tab="worlds"))

    @application.post("/world/delete/<name>")
    @login_required
    def world_delete(name: str):
        if not WORLD_NAME_RE.fullmatch(name):
            abort(404)
        if read_settings()["WORLD_NAME"] == name:
            flash("The active world cannot be deleted. Select another world first.", "error")
            return redirect(url_for("dashboard", tab="worlds"))
        removed = False
        for suffix in (".db", ".fwl", ".db.old", ".fwl.old"):
            candidate = WORLD_DIR / f"{name}{suffix}"
            if candidate.is_file():
                candidate.unlink()
                removed = True
        flash(f"World {name} deleted." if removed else "World files were not found.", "success" if removed else "error")
        return redirect(url_for("dashboard", tab="worlds"))

    @application.get("/world/download/<name>/<extension>")
    @login_required
    def world_download(name: str, extension: str):
        if not WORLD_NAME_RE.fullmatch(name) or extension not in {"db", "fwl"}:
            abort(404)
        path = WORLD_DIR / f"{name}.{extension}"
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @application.post("/world/upload")
    @login_required
    def world_upload():
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
        WORLD_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORLD_DIR) as temporary:
            staging = Path(temporary)
            database.save(staging / database_name)
            descriptor.save(staging / descriptor_name)
            os.replace(staging / database_name, WORLD_DIR / database_name)
            os.replace(staging / descriptor_name, WORLD_DIR / descriptor_name)
            os.chmod(WORLD_DIR / database_name, 0o660)
            os.chmod(WORLD_DIR / descriptor_name, 0o660)
        flash(f"World {world_name} uploaded.", "success")
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
            output = control("restore", name)
            flash(output, "success")
        except (RuntimeError, subprocess.TimeoutExpired) as error:
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
    args = parser.parse_args()
    if args.set_password:
        set_password(*args.set_password)
        return
    parser.error("Start the panel with Waitress or use --set-password.")


if __name__ == "__main__":
    main()
else:
    app = create_app()
