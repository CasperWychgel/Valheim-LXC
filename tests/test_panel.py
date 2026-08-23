from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
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

        def fake_run(command, *, timeout=20):
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
        self.panel_config.write_text(json.dumps(self.panel_payload), encoding="utf-8")
        self.server_config.write_text(self.server_text, encoding="utf-8")
        self.player_log.write_text("", encoding="utf-8")
        for database_file in (
            self.player_db,
            Path(f"{self.player_db}-wal"),
            Path(f"{self.player_db}-shm"),
        ):
            database_file.unlink(missing_ok=True)
        for filename in ("adminlist.txt", "bannedlist.txt", "permittedlist.txt"):
            (self.data / filename).write_text("// Test file\n", encoding="utf-8")
        self.client = self.app.test_client()

    def write_player_log(self, *, disconnected: bool = False) -> None:
        lines = [
            "08/23/2026 14:20:57: Got handshake from client 76561197968825983",
            "08/23/2026 14:21:08: Server: New peer connected,sending global keys",
            "08/23/2026 14:21:17: Got character ZDOID from TeSt : -276917612:4",
            "08/23/2026 14:21:22: Got character ZDOID from TeSt : 0:0",
        ]
        if disconnected:
            lines.extend(
                [
                    "08/23/2026 14:33:09: RPC_Disconnect",
                    "08/23/2026 14:33:09: Closing socket 76561197968825983",
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
        for tab in ("overview", "players", "settings", "access", "worlds", "backups", "logs", "security"):
            with self.subTest(tab=tab):
                response = self.client.get(f"/?tab={tab}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Valheim Admin", response.data)

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
        self.assertIn(b"TeSt", response.data)
        self.assertNotIn(b"Steam_76561197968825983", response.data)

        payload = self.client.get("/api/public-status").get_json()
        self.assertEqual(payload["players"], ["TeSt"])
        self.assertNotIn("platform_id", payload)

    def test_player_activity_tracks_disconnects(self) -> None:
        self.write_player_log(disconnected=True)
        players = self.module.list_players()
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["name"], "TeSt")
        self.assertEqual(players[0]["platform_id"], "Steam_76561197968825983")
        self.assertFalse(players[0]["online"])

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
        self.assertEqual(players[0]["name"], "TeSt")
        self.assertTrue(players[0]["online"])

    def test_authenticated_player_page_and_ban_action(self) -> None:
        self.write_player_log()
        self.login()
        response = self.client.get("/?tab=players")
        self.assertIn(b"Player activity", response.data)
        self.assertIn(b"Steam_76561197968825983", response.data)

        response = self.client.post(
            "/player/Steam_76561197968825983/ban",
            data={"csrf_token": self.csrf(), "action": "ban"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "Steam_76561197968825983",
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

    def test_state_change_requires_csrf(self) -> None:
        self.login()
        response = self.client.post("/action/restart", data={})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
