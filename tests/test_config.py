from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


BASE_ENV = {
    "APP_USERNAME": "owner",
    "APP_PASSWORD": "password",
    "APP_SECRET": "s" * 32,
    "API_KEY": "k" * 24,
    "MAIL_MODE": "direct",
    "IMAP_EMAIL": "owner@qq.com",
    "IMAP_AUTH_CODE": "auth-code",
    "IMAP_PORT": "993",
}


class SettingsTests(unittest.TestCase):
    def test_known_provider_host_is_detected(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.imap_host, "imap.qq.com")

    def test_forward_mode_requires_source_email(self):
        values = {**BASE_ENV, "MAIL_MODE": "forward"}
        with patch.dict(os.environ, values, clear=True), self.assertRaisesRegex(
            RuntimeError, "SOURCE_EMAIL"
        ):
            Settings.from_env()

    def test_non_tls_imap_port_is_rejected(self):
        values = {**BASE_ENV, "IMAP_PORT": "143"}
        with patch.dict(os.environ, values, clear=True), self.assertRaisesRegex(
            RuntimeError, "993"
        ):
            Settings.from_env()


if __name__ == "__main__":
    unittest.main()
