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
        cls.data = root / "data"
        cls.worlds = cls.data / "worlds_local"
        cls.backups = root / "backups"
        cls.server = root / "server"
        for directory in (cls.worlds, cls.backups, cls.server / "steamapps"):
            directory.mkdir(parents=True)

        cls.panel_config = root / "panel.json"
        cls.server_config = root / "server.env"
        password = "Correct-Horse-42"
        iterations = 10_000
        salt = bytes.fromhex("00112233445566778899aabbccddeeff")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        cls.password = password
        cls.panel_config.write_text(
            json.dumps(
                {
                    "username": "admin",
                    "password_hash": f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}",
                    "session_secret": "test-session-secret-that-is-long-enough",
                }
            ),
            encoding="utf-8",
        )
        cls.server_config.write_text(
            '\n'.join(
                [
                    'SERVER_NAME="Test Realm"',
                    'WORLD_NAME="Dedicated"',
                    'SERVER_PASSWORD="TestPass"',
                    'GAME_PORT="2456"',
                    'PUBLIC="0"',
                    'CROSSPLAY="0"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
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
        self.client = self.app.test_client()

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
        for tab in ("overview", "settings", "access", "worlds", "backups", "logs", "security"):
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
