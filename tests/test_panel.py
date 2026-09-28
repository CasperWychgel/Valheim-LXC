from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PANEL_SOURCE = PROJECT_ROOT / "panel" / "app.py"


class PanelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.root = root
        cls.data = root / "data"
        cls.worlds = cls.data / "worlds_local"
        cls.backups = root / "backups"
        cls.server = root / "server"
        for directory in (cls.worlds, cls.backups, cls.server / "steamapps"):
            directory.mkdir(parents=True)

        cls.panel_config = root / "panel.json"
        cls.server_config = root / "server.env"
        cls.player_log = root / "logs" / "valheim.log"
        cls.player_log.parent.mkdir(parents=True)
        cls.player_db = root / "players.sqlite3"
        password = "Correct-Horse-42"
        iterations = 10_000
        salt = bytes.fromhex("00112233445566778899aabbccddeeff")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        cls.password = password
        cls.panel_payload = {
            "username": "admin",
            "password_hash": f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}",
            "session_secret": "test-session-secret-that-is-long-enough",
            "show_player_names_on_login": True,
        }
        cls.panel_config.write_text(json.dumps(cls.panel_payload), encoding="utf-8")
        cls.server_text = '\n'.join(
            [
                'SERVER_NAME="Test Realm"',
                'WORLD_NAME="Dedicated"',
                'SERVER_PASSWORD="TestPass"',
                'GAME_PORT="2456"',
                'PUBLIC="0"',
                'CROSSPLAY="0"',
            ]
        ) + "\n"
        cls.server_config.write_text(cls.server_text, encoding="utf-8")
        cls.player_log.write_text("", encoding="utf-8")
        for filename in ("adminlist.txt", "bannedlist.txt", "permittedlist.txt"):
            (cls.data / filename).write_text("// Test file\n", encoding="utf-8")
        (cls.server / "steamapps" / "appmanifest_896660.acf").write_text(
            '"AppState"\n{\n  "buildid" "123456"\n}\n', encoding="utf-8"
        )

        os.environ.update(
            {
                "VALHEIM_PANEL_CONFIG": str(cls.panel_config),
                "VALHEIM_SERVER_CONFIG": str(cls.server_config),
                "VALHEIM_HOME": str(root),
                "VALHEIM_DATA_DIR": str(cls.data),
                "VALHEIM_WORLD_DIR": str(cls.worlds),
                "VALHEIM_BACKUP_DIR": str(cls.backups),
                "VALHEIM_MANIFEST": str(cls.server / "steamapps" / "appmanifest_896660.acf"),
                "VALHEIM_LOG": str(cls.player_log),
                "VALHEIM_PLAYER_DB": str(cls.player_db),
                "VALHEIM_CTL": "/fake/valheimctl",
            }
        )
        specification = importlib.util.spec_from_file_location("valheim_panel", PANEL_SOURCE)
        assert specification and specification.loader
        cls.module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.module)

        cls.commands: list[list[str]] = []

        def fake_run(command, *, timeout=20):
            cls.commands.append(command.copy())
            if command[:3] == ["systemctl", "is-active", "--quiet"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["systemctl", "show"]:
                if "--property=InvocationID" in command:
                    return subprocess.CompletedProcess(command, 0, "test-invocation\n", "")
                return subprocess.CompletedProcess(command, 0, "Sun 2026-08-23 12:00:00 UTC\n", "")
            if command[:2] == ["hostname", "-I"]:
                return subprocess.CompletedProcess(command, 0, "192.0.2.20\n", "")
            if command and command[0] == "sudo":
                return subprocess.CompletedProcess(command, 0, "Operation completed.\n", "")
            return subprocess.CompletedProcess(command, 1, "", "not available")

        cls.module.run = fake_run
        cls.app = cls.module.create_app()
        cls.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.commands.clear()
        self.panel_config.write_text(json.dumps(self.panel_payload), encoding="utf-8")
        self.server_config.write_text(self.server_text, encoding="utf-8")
        self.player_log.write_text("", encoding="utf-8")
        for world_file in self.worlds.iterdir():
            if world_file.is_dir():
                shutil.rmtree(world_file)
            else:
                world_file.unlink()
        for backup_file in self.backups.iterdir():
            if backup_file.is_dir():
                shutil.rmtree(backup_file)
            else:
                backup_file.unlink()
        for database_file in (
            self.player_db,
            Path(f"{self.player_db}-wal"),
            Path(f"{self.player_db}-shm"),
        ):
            database_file.unlink(missing_ok=True)
        for filename in ("adminlist.txt", "bannedlist.txt", "permittedlist.txt"):
            (self.data / filename).write_text("// Test file\n", encoding="utf-8")
        self.client = self.app.test_client()

    @staticmethod
    def archive_files(world_name: str = "QAWorld", generation: int = 7) -> dict[str, bytes]:
        prefix = f"{world_name}/"
        return {
            f"{prefix}_main.{generation}.db2": b"synthetic world database",
            f"{prefix}_main.{generation}.fwl2": b"synthetic world descriptor",
            f"{prefix}_main.{generation}.chunks": b"synthetic chunk index",
            f"{prefix}_main.{generation}.ok": b"ok\n",
            f"{prefix}00_00__0_{generation}.chunk": b"synthetic chunk data",
        }

    @staticmethod
    def tar_bytes(files: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> io.BytesIO:
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w:gz") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o640
                archive.addfile(member, io.BytesIO(content))
            if symlink:
                member = tarfile.TarInfo(symlink[0])
                member.type = tarfile.SYMTYPE
                member.linkname = symlink[1]
                archive.addfile(member)
        result.seek(0)
        return result

    @staticmethod
    def zip_bytes(files: dict[str, bytes]) -> io.BytesIO:
        result = io.BytesIO()
        with zipfile.ZipFile(result, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        result.seek(0)
        return result

    @staticmethod
    def mod_zip_bytes(mod_name: str = "SampleMod") -> io.BytesIO:
        return PanelIntegrationTests.zip_bytes(
            {
                f"BepInEx/plugins/{mod_name}/{mod_name}.dll": b"synthetic plugin",
                f"BepInEx/config/{mod_name}.cfg": b"## sample config\n",
            }
        )

    @classmethod
    def backup_bytes(cls, world_name: str = "ArchiveRealm") -> io.BytesIO:
        world_files = {
            f"worlds_local/{name}": content
            for name, content in cls.archive_files(world_name, generation=3).items()
        }
        world_files.update(
            {
                "adminlist.txt": b"// Synthetic test data\n",
                "bannedlist.txt": b"// Synthetic test data\n",
                "permittedlist.txt": b"// Synthetic test data\n",
            }
        )
        return cls.tar_bytes(world_files)

    def write_chunked_world(self, world_name: str, generation: int = 7) -> Path:
        root = self.worlds / world_name
        root.mkdir()
        for archive_name, content in self.archive_files(world_name, generation).items():
            (root / Path(archive_name).name).write_bytes(content)
        return root

    def write_player_log(self, *, disconnected: bool = False) -> None:
        lines = [
            "08/23/2026 14:20:57: Got handshake from client 99999999999999999",
            "08/23/2026 14:21:08: Server: New peer connected,sending global keys",
            "08/23/2026 14:21:17: Got character ZDOID from TestViking : 123456789:4",
            "08/23/2026 14:21:22: Got character ZDOID from TestViking : 0:0",
        ]
        if disconnected:
            lines.extend(
                [
                    "08/23/2026 14:33:09: RPC_Disconnect",
                    "08/23/2026 14:33:09: Closing socket 99999999999999999",
                ]
            )
        self.player_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def login(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": self.password},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Current world", response.data)

    def csrf(self) -> str:
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_login_rejects_bad_password(self) -> None:
        response = self.client.post("/login", data={"username": "admin", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Incorrect username or password", response.data)

    def test_all_panel_tabs_render(self) -> None:
        self.login()
        for tab in ("overview", "players", "settings", "access", "worlds", "backups", "logs", "mods", "security"):
            with self.subTest(tab=tab):
                response = self.client.get(f"/?tab={tab}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Valheim Admin", response.data)

    def test_mod_runtime_toggle(self) -> None:
        self.login()
        response = self.client.post(
            "/mods/settings",
            data={"csrf_token": self.csrf(), "MODS_ENABLED": "1"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mod runtime setting saved", response.data)
        self.assertEqual(self.module.read_settings()["MODS_ENABLED"], "1")

    def test_manual_mod_upload_toggle_and_uninstall(self) -> None:
        self.login()
        response = self.client.post(
            "/mods/upload",
            data={
                "csrf_token": self.csrf(),
                "package_name": "ManualPack",
                "mod_archive": (self.mod_zip_bytes("ManualPack"), "ManualPack.zip"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Uploaded and installed", response.data)

        registry = json.loads((self.data / "mods" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(len(registry["mods"]), 1)
        mod_key = registry["mods"][0]["key"]
        self.assertTrue((self.data / "mods" / "plugins" / mod_key).is_dir())

        response = self.client.post(
            f"/mods/toggle/{mod_key}",
            data={"csrf_token": self.csrf(), "action": "disable"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        registry = json.loads((self.data / "mods" / "registry.json").read_text(encoding="utf-8"))
        self.assertFalse(registry["mods"][0]["enabled"])
        self.assertTrue((self.data / "mods" / "plugins-disabled" / mod_key).is_dir())

        response = self.client.post(
            f"/mods/toggle/{mod_key}",
            data={"csrf_token": self.csrf(), "action": "enable"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        registry = json.loads((self.data / "mods" / "registry.json").read_text(encoding="utf-8"))
        self.assertTrue(registry["mods"][0]["enabled"])
        self.assertTrue((self.data / "mods" / "plugins" / mod_key).is_dir())

        response = self.client.post(
            f"/mods/uninstall/{mod_key}",
            data={"csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Removed ManualPack", response.data)
        registry = json.loads((self.data / "mods" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["mods"], [])

    def test_provider_mod_install_resolves_dependencies(self) -> None:
        self.login()
        original_fetch = self.module.fetch_provider_package
        original_download = self.module.http_download_file

        def fake_fetch(provider, community, namespace, name, version):
            if namespace == "RootTeam" and name == "RootMod":
                return {
                    "provider": provider,
                    "community": community,
                    "namespace": namespace,
                    "name": name,
                    "version": "1.0.0",
                    "download_url": "https://example.invalid/root.zip",
                    "dependencies": [("DepTeam", "DepMod", "2.0.0")],
                }
            if namespace == "DepTeam" and name == "DepMod":
                return {
                    "provider": provider,
                    "community": community,
                    "namespace": namespace,
                    "name": name,
                    "version": "2.0.0",
                    "download_url": "https://example.invalid/dep.zip",
                    "dependencies": [],
                }
            raise ValueError("unexpected package request")

        def fake_download(url, destination, *, timeout=90):
            if "dep.zip" in url:
                payload = self.mod_zip_bytes("DepMod").getvalue()
            else:
                payload = self.mod_zip_bytes("RootMod").getvalue()
            destination.write_bytes(payload)

        self.module.fetch_provider_package = fake_fetch
        self.module.http_download_file = fake_download
        try:
            response = self.client.post(
                "/mods/install/provider",
                data={
                    "csrf_token": self.csrf(),
                    "provider": "thunderstore",
                    "namespace": "RootTeam",
                    "package_name": "RootMod",
                    "version": "",
                },
                follow_redirects=True,
            )
        finally:
            self.module.fetch_provider_package = original_fetch
            self.module.http_download_file = original_download

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Installed packages", response.data)
        registry = json.loads((self.data / "mods" / "registry.json").read_text(encoding="utf-8"))
        keys = {entry["key"] for entry in registry["mods"]}
        self.assertIn("thunderstore--rootteam--rootmod", keys)
        self.assertIn("thunderstore--depteam--depmod", keys)

    def test_status_api(self) -> None:
        self.login()
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["active"])
        self.assertEqual(payload["build"], "123456")
        self.assertEqual(payload["address"], "192.0.2.20")

    def test_public_summary_shows_active_name_but_not_game_id(self) -> None:
        self.write_player_log()
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Realm", response.data)
        self.assertIn(b"1 / 10 Players", response.data)
        self.assertIn(b"TestViking", response.data)
        self.assertNotIn(b"Steam_99999999999999999", response.data)

        payload = self.client.get("/api/public-status").get_json()
        self.assertEqual(payload["players"], ["TestViking"])
        self.assertNotIn("platform_id", payload)

    def test_player_activity_tracks_disconnects(self) -> None:
        self.write_player_log(disconnected=True)
        players = self.module.list_players()
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["name"], "TestViking")
        self.assertEqual(players[0]["platform_id"], "Steam_99999999999999999")
        self.assertFalse(players[0]["online"])
        self.assertEqual(self.module.online_player_count(), 0)

    def test_strict_online_player_count(self) -> None:
        self.write_player_log()
        self.assertEqual(self.module.online_player_count(), 1)

    def test_parser_upgrade_replays_log_when_history_is_empty(self) -> None:
        self.write_player_log()
        stat = self.player_log.stat()
        connection = self.module.player_database()
        self.module.set_tracker_meta(connection, "parser_version", "1")
        self.module.set_tracker_meta(connection, "log_signature", f"{stat.st_dev}:{stat.st_ino}")
        self.module.set_tracker_meta(connection, "log_offset", str(stat.st_size))
        connection.commit()
        connection.close()

        players = self.module.list_players()
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["name"], "TestViking")
        self.assertTrue(players[0]["online"])

    def test_authenticated_player_page_and_ban_action(self) -> None:
        self.write_player_log()
        self.login()
        response = self.client.get("/?tab=players")
        self.assertIn(b"Player activity", response.data)
        self.assertIn(b"Steam_99999999999999999", response.data)

        response = self.client.post(
            "/player/Steam_99999999999999999/ban",
            data={"csrf_token": self.csrf(), "action": "ban"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "Steam_99999999999999999",
            (self.data / "bannedlist.txt").read_text(encoding="utf-8"),
        )
        player_payload = self.client.get("/api/players").get_json()["players"][0]
        self.assertTrue(player_payload["banned"])
        self.assertEqual(player_payload["role"], "Banned")

    def test_login_name_visibility_can_be_disabled(self) -> None:
        self.write_player_log()
        self.login()
        response = self.client.post(
            "/security/privacy",
            data={"csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 302)
        payload = self.client.get("/api/public-status").get_json()
        self.assertEqual(payload["player_count"], 1)
        self.assertEqual(payload["players"], [])

    def test_settings_and_access_save(self) -> None:
        self.login()
        token = self.csrf()
        settings = {
            "csrf_token": token,
            "SERVER_NAME": "QA Realm",
            "WORLD_NAME": "Dedicated",
            "SERVER_PASSWORD": "Secret42",
            "GAME_PORT": "2456",
            "PUBLIC": "1",
            "SAVE_INTERVAL": "1800",
            "BACKUPS": "4",
            "BACKUP_SHORT": "7200",
            "BACKUP_LONG": "43200",
            "PRESET": "Normal",
            "COMBAT": "hard",
            "DEATH_PENALTY": "",
            "RESOURCES": "more",
            "RAIDS": "",
            "PORTALS": "",
        }
        response = self.client.post("/settings", data=settings)
        self.assertEqual(response.status_code, 302)
        parsed = self.module.read_settings()
        self.assertEqual(parsed["SERVER_NAME"], "QA Realm")
        self.assertEqual(parsed["PUBLIC"], "1")
        self.assertEqual(parsed["COMBAT"], "hard")

        response = self.client.post(
            "/access/admins",
            data={"csrf_token": token, "entries": "// Maintainers\nSteam_123456\nSteam_123456\n"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.data / "adminlist.txt").read_text(encoding="utf-8").count("Steam_123456"), 1)

    def test_world_creation_snapshots_and_restarts(self) -> None:
        self.login()
        response = self.client.post(
            "/world/create",
            data={"csrf_token": self.csrf(), "world_name": "Fresh-Realm"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Valheim is generating its new random seed", response.data)
        self.assertEqual(self.module.read_settings()["WORLD_NAME"], "Fresh-Realm")
        control_actions = [
            command[3]
            for command in self.commands
            if command[:3] == ["sudo", "-n", "/fake/valheimctl"]
        ]
        self.assertEqual(control_actions[-2:], ["backup", "restart"])

    def test_failed_world_restart_restores_the_previous_configuration(self) -> None:
        self.login()
        original_run = self.module.run
        restart_calls = 0

        def fail_first_restart(command, *, timeout=20):
            nonlocal restart_calls
            if command[:4] == ["sudo", "-n", "/fake/valheimctl", "restart"]:
                restart_calls += 1
                if restart_calls == 1:
                    return subprocess.CompletedProcess(command, 1, "", "restart failed")
            return original_run(command, timeout=timeout)

        self.module.run = fail_first_restart
        try:
            response = self.client.post(
                "/world/create",
                data={"csrf_token": self.csrf(), "world_name": "Rollback-Test"},
                follow_redirects=True,
            )
        finally:
            self.module.run = original_run

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"previous world configuration was restored and restarted", response.data)
        self.assertEqual(self.module.read_settings()["WORLD_NAME"], "Dedicated")
        self.assertEqual(restart_calls, 2)

    def test_world_creation_is_blocked_while_a_player_is_online(self) -> None:
        self.write_player_log()
        self.login()
        response = self.client.post(
            "/world/create",
            data={"csrf_token": self.csrf(), "world_name": "Unsafe-Switch"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"blocked while 1 player(s) are online", response.data)
        self.assertEqual(self.module.read_settings()["WORLD_NAME"], "Dedicated")
        self.assertFalse(
            any(
                command[:4] == ["sudo", "-n", "/fake/valheimctl", "restart"]
                for command in self.commands
            )
        )

    def test_world_selection_requires_a_complete_pair(self) -> None:
        (self.worlds / "Incomplete.db").write_bytes(b"database")
        self.login()
        response = self.client.post(
            "/world/select/Incomplete",
            data={"csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.module.read_settings()["WORLD_NAME"], "Dedicated")

    def test_world_upload_does_not_overwrite_an_existing_world(self) -> None:
        (self.worlds / "Existing.db").write_bytes(b"original database")
        (self.worlds / "Existing.fwl").write_bytes(b"original descriptor")
        self.login()
        response = self.client.post(
            "/world/upload",
            data={
                "csrf_token": self.csrf(),
                "database": (io.BytesIO(b"replacement database"), "Existing.db"),
                "descriptor": (io.BytesIO(b"replacement descriptor"), "Existing.fwl"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A world with this name already exists", response.data)
        self.assertEqual((self.worlds / "Existing.db").read_bytes(), b"original database")
        self.assertEqual((self.worlds / "Existing.fwl").read_bytes(), b"original descriptor")

    def test_valheim_1_tar_world_import_and_library_detection(self) -> None:
        self.login()
        response = self.client.post(
            "/world/upload/archive",
            data={
                "csrf_token": self.csrf(),
                "world_archive": (self.tar_bytes(self.archive_files()), "QAWorld.tar.gz"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Valheim 1.0 world QAWorld imported", response.data)
        self.assertTrue((self.worlds / "QAWorld" / "_main.7.db2").is_file())

        worlds = self.module.list_worlds()
        self.assertEqual(len(worlds), 1)
        self.assertEqual(worlds[0]["name"], "QAWorld")
        self.assertEqual(worlds[0]["kind"], "chunked")
        self.assertEqual(worlds[0]["generation"], 7)
        self.assertEqual(worlds[0]["chunks"], 1)
        self.assertTrue(worlds[0]["complete"])

        auto_backup = self.worlds / "QAWorld_backup_auto-20260101-010203"
        shutil.copytree(self.worlds / "QAWorld", auto_backup)
        self.assertEqual([world["name"] for world in self.module.list_worlds()], ["QAWorld"])

    def test_valheim_1_zip_world_import(self) -> None:
        self.login()
        response = self.client.post(
            "/world/upload/archive",
            data={
                "csrf_token": self.csrf(),
                "world_archive": (self.zip_bytes(self.archive_files("ZipRealm")), "ZipRealm.zip"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Valheim 1.0 world ZipRealm imported", response.data)
        self.assertTrue((self.worlds / "ZipRealm" / "_main.7.ok").is_file())

    def test_valheim_1_world_archive_does_not_overwrite_and_can_be_downloaded(self) -> None:
        world = self.write_chunked_world("StoredRealm", generation=5)
        original = (world / "_main.5.db2").read_bytes()
        self.login()
        response = self.client.post(
            "/world/upload/archive",
            data={
                "csrf_token": self.csrf(),
                "world_archive": (
                    self.tar_bytes(self.archive_files("StoredRealm", generation=8)),
                    "StoredRealm.tar.gz",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Existing worlds are never overwritten", response.data)
        self.assertEqual((world / "_main.5.db2").read_bytes(), original)

        response = self.client.get("/world/download/StoredRealm/archive")
        self.assertEqual(response.status_code, 200)
        payload = response.data
        response.close()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            names = archive.getnames()
        self.assertIn("StoredRealm/_main.5.db2", names)
        self.assertFalse(any(name.startswith("worlds_local/") for name in names))

    def test_inactive_valheim_1_world_delete_creates_snapshot(self) -> None:
        self.write_chunked_world("DisposableRealm")
        self.login()
        response = self.client.post(
            "/world/delete/DisposableRealm",
            data={"csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"deleted after creating a safety snapshot", response.data)
        self.assertFalse((self.worlds / "DisposableRealm").exists())
        self.assertTrue(
            any(command[:4] == ["sudo", "-n", "/fake/valheimctl", "backup"] for command in self.commands)
        )

    def test_world_archive_rejects_traversal_and_links(self) -> None:
        self.login()
        traversal_files = self.archive_files("SafeRealm")
        traversal_files["../outside.txt"] = b"must not escape"
        response = self.client.post(
            "/world/upload/archive",
            data={
                "csrf_token": self.csrf(),
                "world_archive": (self.tar_bytes(traversal_files), "unsafe.tar.gz"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"unsafe path", response.data)
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertFalse((self.worlds / "SafeRealm").exists())

        response = self.client.post(
            "/world/upload/archive",
            data={
                "csrf_token": self.csrf(),
                "world_archive": (
                    self.tar_bytes(
                        self.archive_files("LinkedRealm"),
                        symlink=("LinkedRealm/latest", "_main.7.db2"),
                    ),
                    "linked.tar.gz",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"links or unsupported file types", response.data)
        self.assertFalse((self.worlds / "LinkedRealm").exists())

    def test_legacy_world_is_marked_for_migration_and_activated(self) -> None:
        self.login()
        response = self.client.post(
            "/world/upload/legacy",
            data={
                "csrf_token": self.csrf(),
                "database": (io.BytesIO(b"synthetic legacy database"), "LegacyRealm.db"),
                "descriptor": (io.BytesIO(b"synthetic legacy descriptor"), "LegacyRealm.fwl"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Upload for migration", response.data)
        world = self.module.find_world("LegacyRealm")
        self.assertIsNotNone(world)
        self.assertEqual(world["kind"], "legacy")
        self.assertTrue(world["complete"])

        response = self.client.post(
            "/world/select/LegacyRealm",
            data={"csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"Valheim 1.0 is migrating it", response.data)
        self.assertEqual(self.module.read_settings()["WORLD_NAME"], "LegacyRealm")

    def test_portable_backup_upload_is_validated_before_restore(self) -> None:
        self.login()
        backup_name = "valheim-20260101T010203Z.tar.gz"
        response = self.client.post(
            "/backup/upload",
            data={
                "csrf_token": self.csrf(),
                "backup_archive": (self.backup_bytes("Dedicated"), backup_name),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Server backup uploaded and validated", response.data)
        self.assertTrue((self.backups / backup_name).is_file())
        self.assertFalse(any(command[-2:] == ["restore", backup_name] for command in self.commands))

        response = self.client.post(
            f"/backup/restore/{backup_name}",
            data={"csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(command[-2:] == ["restore", backup_name] for command in self.commands))

    def test_portable_restore_is_blocked_while_players_are_online(self) -> None:
        backup_name = "valheim-20260103T010203Z.tar.gz"
        (self.backups / backup_name).write_bytes(self.backup_bytes("Dedicated").getvalue())
        self.write_player_log()
        self.login()
        response = self.client.post(
            f"/backup/restore/{backup_name}",
            data={"csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"blocked while 1 player(s) are online", response.data)
        self.assertFalse(any(command[-2:] == ["restore", backup_name] for command in self.commands))

    def test_portable_restore_requires_the_selected_world(self) -> None:
        backup_name = "valheim-20260104T010203Z.tar.gz"
        (self.backups / backup_name).write_bytes(self.backup_bytes("DifferentRealm").getvalue())
        self.login()
        response = self.client.post(
            f"/backup/restore/{backup_name}",
            data={"csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"does not contain the currently selected world Dedicated", response.data)
        self.assertFalse(any(command[-2:] == ["restore", backup_name] for command in self.commands))

    def test_portable_backup_rejects_server_configuration(self) -> None:
        self.login()
        backup_name = "valheim-20260102T010203Z.tar.gz"
        files = {
            f"worlds_local/{name}": content
            for name, content in self.archive_files("PrivateRealm", generation=4).items()
        }
        files.update(
            {
                "adminlist.txt": b"// Synthetic test data\n",
                "bannedlist.txt": b"// Synthetic test data\n",
                "permittedlist.txt": b"// Synthetic test data\n",
                "server.env": b'SERVER_NAME="MustNotImport"\n',
            }
        )
        response = self.client.post(
            "/backup/upload",
            data={
                "csrf_token": self.csrf(),
                "backup_archive": (self.tar_bytes(files), backup_name),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"outside the portable backup layout", response.data)
        self.assertFalse((self.backups / backup_name).exists())

    def test_portable_backup_validation_accepts_managed_mods(self) -> None:
        backup_name = "valheim-20260203T010203Z.tar.gz"
        files = {
            f"worlds_local/{name}": content
            for name, content in self.archive_files("ModdedRealm", generation=5).items()
        }
        files.update(
            {
                "adminlist.txt": b"// Synthetic test data\n",
                "bannedlist.txt": b"// Synthetic test data\n",
                "permittedlist.txt": b"// Synthetic test data\n",
                "mods/plugins/testmod/TestMod.dll": b"plugin",
            }
        )
        archive = self.backups / backup_name
        archive.write_bytes(self.tar_bytes(files).getvalue())
        metadata = self.module.validate_backup_archive(archive)
        self.assertGreater(metadata["mod_files"], 0)

    def test_settings_cannot_bypass_safe_world_switching(self) -> None:
        self.login()
        settings = {
            "csrf_token": self.csrf(),
            "SERVER_NAME": "Test Realm",
            "WORLD_NAME": "Bypass",
            "SERVER_PASSWORD": "TestPass",
            "GAME_PORT": "2456",
            "PUBLIC": "0",
            "SAVE_INTERVAL": "1800",
            "BACKUPS": "4",
            "BACKUP_SHORT": "7200",
            "BACKUP_LONG": "43200",
            "PRESET": "Normal",
            "COMBAT": "",
            "DEATH_PENALTY": "",
            "RESOURCES": "",
            "RAIDS": "",
            "PORTALS": "",
        }
        response = self.client.post("/settings", data=settings, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Use the Worlds page to change the active world safely", response.data)
        self.assertEqual(self.module.read_settings()["WORLD_NAME"], "Dedicated")

    def test_state_change_requires_csrf(self) -> None:
        self.login()
        response = self.client.post("/action/restart", data={})
        self.assertEqual(response.status_code, 400)


class SetupDefinitionTests(unittest.TestCase):
    def test_player_aware_daily_maintenance_units_are_installed(self) -> None:
        setup = (PROJECT_ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn('cat >"$VALHEIM_HOME/bin/daily-maintenance"', setup)
        self.assertIn("OnCalendar=*-*-* *:00:00", setup)
        self.assertIn("OnCalendar=*-*-* *:30:00", setup)
        self.assertIn("update-server\" --defer-if-players", setup)
        self.assertIn("systemctl disable --now valheim-update.timer", setup)
        self.assertIn("valheim-maintenance.timer", setup)

    def test_portable_restore_is_validated_snapshotted_and_transactional(self) -> None:
        setup = (PROJECT_ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn('install -o root -g root -m 0600 "$archive" "$archive_copy"', setup)
        self.assertIn('--validate-backup-archive "$archive_copy"', setup)
        self.assertIn("/opt/valheim/bin/backup-server", setup)
        self.assertIn("restore-rollback", setup)
        self.assertIn("Server and panel settings were unchanged", setup)


if __name__ == "__main__":
    unittest.main()
