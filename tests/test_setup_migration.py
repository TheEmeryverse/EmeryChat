import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import setup_emery


class TestSetupMigration(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.config_dir = self.base_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.original_paths = {
            "BASE_DIR": setup_emery.BASE_DIR,
            "CONFIG_DIR": setup_emery.CONFIG_DIR,
            "ENV_PATH": setup_emery.ENV_PATH,
            "USERS_CONFIG_PATH": setup_emery.USERS_CONFIG_PATH,
            "INTEGRATIONS_CONFIG_PATH": setup_emery.INTEGRATIONS_CONFIG_PATH,
            "NEWS_FEEDS_CONFIG_PATH": setup_emery.NEWS_FEEDS_CONFIG_PATH,
            "WEATHER_LOCATIONS_PATH": setup_emery.WEATHER_LOCATIONS_PATH,
            "CUSTOM_JOBS_PATH": setup_emery.CUSTOM_JOBS_PATH,
            "MEMORY_PATH": setup_emery.MEMORY_PATH,
            "CAMERA_LOG_PATH": setup_emery.CAMERA_LOG_PATH,
        }

        setup_emery.BASE_DIR = self.base_dir
        setup_emery.CONFIG_DIR = self.config_dir
        setup_emery.ENV_PATH = self.base_dir / ".env"
        setup_emery.USERS_CONFIG_PATH = self.config_dir / "users.json"
        setup_emery.INTEGRATIONS_CONFIG_PATH = self.config_dir / "integrations.json"
        setup_emery.NEWS_FEEDS_CONFIG_PATH = self.config_dir / "news_feeds.json"
        setup_emery.WEATHER_LOCATIONS_PATH = self.config_dir / "weather_locations.json"
        setup_emery.CUSTOM_JOBS_PATH = self.config_dir / "custom_jobs.json"
        setup_emery.MEMORY_PATH = self.base_dir / "memory.md"
        setup_emery.CAMERA_LOG_PATH = self.base_dir / "camera_log.md"

    def tearDown(self):
        for key, value in self.original_paths.items():
            setattr(setup_emery, key, value)
        self.tempdir.cleanup()

    def _write_json(self, path: Path, payload):
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_default_json_placeholders_do_not_block_env_seed(self):
        setup_emery.ENV_PATH.write_text(
            "\n".join(
                [
                    "PRIMARY_USER_ID=123",
                    "USER_NAME=Hudson",
                    "USER_LOCATION=Waukesha, WI",
                    "USER_TIMEZONE=America/Chicago",
                    "SECONDARY_USER_ID=456",
                    "USER_2_NAME=Anyssa",
                    "GOOGLE_CALENDAR_IDS=primary,family@example.com",
                    "TELEGRAM_GROUP_CHAT_ID=-1001",
                    "REOLINK_CAMERAS=frontdoor:0,backdoor:1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_json(setup_emery.USERS_CONFIG_PATH, setup_emery.clone_default(setup_emery.DEFAULT_USERS))
        self._write_json(
            setup_emery.INTEGRATIONS_CONFIG_PATH,
            setup_emery.clone_default(setup_emery.DEFAULT_INTEGRATIONS),
        )
        self._write_json(
            setup_emery.NEWS_FEEDS_CONFIG_PATH,
            setup_emery.clone_default(setup_emery.DEFAULT_NEWS_FEEDS),
        )

        env_seed, users_seed, integrations_seed, _ = setup_emery.derive_seed(
            SimpleNamespace(import_env=None, fresh=False)
        )

        self.assertEqual(env_seed["USER_NAME"], "Hudson")
        self.assertEqual(users_seed["primary_user"]["id"], 123)
        self.assertEqual(users_seed["primary_user"]["name"], "Hudson")
        self.assertEqual(users_seed["primary_user"]["location"], "Waukesha, WI")
        self.assertEqual(users_seed["primary_user"]["timezone"], "America/Chicago")
        self.assertEqual(users_seed["secondary_user"]["id"], 456)
        self.assertEqual(users_seed["secondary_user"]["name"], "Anyssa")
        self.assertEqual(
            integrations_seed["google_calendar_ids"],
            ["primary", "family@example.com"],
        )
        self.assertEqual(integrations_seed["telegram"]["group_chat_id"], -1001)
        self.assertEqual(
            integrations_seed["reolink"]["cameras"],
            {"frontdoor": "0", "backdoor": "1"},
        )

    def test_malformed_reolink_json_falls_back_to_env_seed(self):
        setup_emery.ENV_PATH.write_text(
            "REOLINK_CAMERAS=frontdoor:0,backdoor:1\n",
            encoding="utf-8",
        )
        self._write_json(
            setup_emery.INTEGRATIONS_CONFIG_PATH,
            {
                "google_calendar_ids": ["primary"],
                "telegram": {},
                "reolink": {
                    "cameras": {"front": "not-a-channel"},
                    "camera_descriptions": {},
                },
                "nest": {},
            },
        )

        _, _, integrations_seed, _ = setup_emery.derive_seed(
            SimpleNamespace(import_env=None, fresh=False)
        )

        self.assertEqual(
            integrations_seed["reolink"]["cameras"],
            {"frontdoor": "0", "backdoor": "1"},
        )

    def test_parse_name_map_preserves_commas_inside_values(self):
        parsed = setup_emery.parse_name_map(
            "front:A high up, downward angled view, back:Rear patio and gate"
        )

        self.assertEqual(
            parsed,
            {
                "front": "A high up, downward angled view",
                "back": "Rear patio and gate",
            },
        )


if __name__ == "__main__":
    unittest.main()
