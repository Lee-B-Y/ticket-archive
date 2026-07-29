from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}
KNOWN_IMAP_HOSTS = {
    "qq.com": "imap.qq.com",
    "foxmail.com": "imap.qq.com",
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
    "yeah.net": "imap.yeah.net",
    "gmail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
}


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in TRUE_VALUES


def _imap_host(address: str) -> str:
    configured = os.getenv("IMAP_HOST", "").strip()
    if configured:
        return configured
    domain = address.rpartition("@")[2].casefold()
    return KNOWN_IMAP_HOSTS.get(domain, "")


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    secret: str
    api_key: str
    cookie_secure: bool
    mail_mode: str
    imap_email: str
    imap_auth_code: str
    imap_host: str
    imap_port: int
    imap_folder: str
    source_email: str
    auto_confirm_forwarding: bool
    sync_on_start: bool
    sync_interval_minutes: int
    data_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        address = os.getenv("IMAP_EMAIL", "").strip()
        mode = os.getenv("MAIL_MODE", "direct").strip().casefold()
        try:
            interval = max(5, int(os.getenv("SYNC_INTERVAL_MINUTES", "1440")))
            port = int(os.getenv("IMAP_PORT", "993"))
        except ValueError as exc:
            raise RuntimeError("IMAP_PORT and SYNC_INTERVAL_MINUTES must be integers") from exc
        settings = cls(
            username=os.getenv("APP_USERNAME", "admin").strip(),
            password=os.getenv("APP_PASSWORD", ""),
            secret=os.getenv("APP_SECRET", ""),
            api_key=os.getenv("API_KEY", ""),
            cookie_secure=_boolean("COOKIE_SECURE", False),
            mail_mode=mode,
            imap_email=address,
            imap_auth_code=os.getenv("IMAP_AUTH_CODE", ""),
            imap_host=_imap_host(address),
            imap_port=port,
            imap_folder=os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX",
            source_email=os.getenv("SOURCE_EMAIL", "").strip().casefold(),
            auto_confirm_forwarding=_boolean("AUTO_CONFIRM_FORWARDING", True),
            sync_on_start=_boolean("SYNC_ON_START", True),
            sync_interval_minutes=interval,
            data_dir=Path(os.getenv("DATA_DIR", "/data")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        errors: list[str] = []
        if self.mail_mode not in {"direct", "forward"}:
            errors.append("MAIL_MODE must be direct or forward")
        if not self.username or not self.password:
            errors.append("APP_USERNAME and APP_PASSWORD are required")
        if len(self.secret) < 32:
            errors.append("APP_SECRET must contain at least 32 characters")
        if len(self.api_key) < 24:
            errors.append("API_KEY must contain at least 24 characters")
        if not self.imap_email or "@" not in self.imap_email:
            errors.append("IMAP_EMAIL is required")
        if not self.imap_auth_code:
            errors.append("IMAP_AUTH_CODE is required")
        if not self.imap_host:
            errors.append("IMAP_HOST is required when the provider cannot be detected")
        if self.imap_port != 993:
            errors.append("only IMAP TLS port 993 is supported")
        if self.mail_mode == "forward" and (not self.source_email or "@" not in self.source_email):
            errors.append("SOURCE_EMAIL is required in forward mode")
        if errors:
            raise RuntimeError("; ".join(errors))

    @property
    def database(self) -> Path:
        return self.data_dir / "tickets.sqlite3"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw-eml"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "sync-state.json"
