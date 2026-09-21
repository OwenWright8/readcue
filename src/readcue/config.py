"""Configuration, read entirely from environment variables (see .env.example)."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from .errors import ConfigError

PROVIDERS = ("ollama", "anthropic")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TRUE = {"1", "true", "yes", "on"}


@dataclass
class Config:
    provider: str = "anthropic"

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_num_ctx: int = 16384
    ollama_timeout: int = 900
    ollama_api_key: str = field(default="", repr=False)

    anthropic_model: str = "claude-sonnet-5"
    anthropic_max_tokens: int = 16000
    anthropic_api_key: str = field(default="", repr=False)  # empty: the SDK uses its own credential lookup

    pushover_app_token: str = field(default="", repr=False)
    pushover_user_key: str = field(default="", repr=False)
    pushover_device: str = ""
    pushover_priority: int = 0

    notify_days_before: int = 1
    notify_time: time = time(8, 0)
    check_interval_seconds: int = 60
    summarize_days_before: int = 3  # don't spend tokens on a chapter until it is due within this many days
    max_upload_mb: int = 1024  # per request; a whole scanned textbook can be several hundred MB
    ocr_lang: str = "eng"  # tesseract language code(s), e.g. "eng+spa"

    data_dir: Path = Path("data")
    base_url: str = ""  # public URL of this app, used for links in notifications
    username: str = "readcue"
    password: str = field(default="", repr=False)  # empty disables HTTP basic auth
    bind: str = ""  # address the app is published on (READCUE_BIND); decides whether a password is required
    insecure_no_auth: bool = (
        False  # READCUE_INSECURE_NO_AUTH: allow a network-reachable app without a password
    )
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "readcue.db"

    @property
    def model_name(self) -> str:
        return self.ollama_model if self.provider == "ollama" else self.anthropic_model

    def link(self, path: str) -> str | None:
        """Absolute URL for a path on this app, or None if READCUE_BASE_URL isn't set."""
        return f"{self.base_url.rstrip('/')}{path}" if self.base_url else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        cfg = cls()

        def get(name: str) -> str | None:
            value = (env.get(name) or "").strip()
            return value or None

        def number(name: str, attr: str) -> None:
            if (raw := get(name)) is not None:
                try:
                    setattr(cfg, attr, int(raw))
                except ValueError:
                    raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None

        for name, attr in [
            ("READCUE_LLM_PROVIDER", "provider"),
            ("OLLAMA_HOST", "ollama_host"),
            ("OLLAMA_MODEL", "ollama_model"),
            ("OLLAMA_API_KEY", "ollama_api_key"),
            ("ANTHROPIC_MODEL", "anthropic_model"),
            ("ANTHROPIC_API_KEY", "anthropic_api_key"),
            ("PUSHOVER_APP_TOKEN", "pushover_app_token"),
            ("PUSHOVER_USER_KEY", "pushover_user_key"),
            ("PUSHOVER_DEVICE", "pushover_device"),
            ("READCUE_BASE_URL", "base_url"),
            ("READCUE_OCR_LANG", "ocr_lang"),
            ("READCUE_USERNAME", "username"),
            ("READCUE_PASSWORD", "password"),
            ("READCUE_BIND", "bind"),
            ("READCUE_LOG_LEVEL", "log_level"),
        ]:
            if (value := get(name)) is not None:
                setattr(cfg, attr, value)

        number("OLLAMA_NUM_CTX", "ollama_num_ctx")
        number("OLLAMA_TIMEOUT", "ollama_timeout")
        number("ANTHROPIC_MAX_TOKENS", "anthropic_max_tokens")
        number("PUSHOVER_PRIORITY", "pushover_priority")
        number("READCUE_NOTIFY_DAYS_BEFORE", "notify_days_before")
        number("READCUE_CHECK_INTERVAL", "check_interval_seconds")
        number("READCUE_SUMMARIZE_DAYS_BEFORE", "summarize_days_before")
        number("READCUE_MAX_UPLOAD_MB", "max_upload_mb")

        if (raw := get("READCUE_NOTIFY_TIME")) is not None:
            try:
                cfg.notify_time = time.fromisoformat(raw)
            except ValueError:
                raise ConfigError(f"READCUE_NOTIFY_TIME must look like 08:00, got {raw!r}") from None
        if (raw := get("READCUE_DATA_DIR")) is not None:
            cfg.data_dir = Path(raw).expanduser()

        cfg.insecure_no_auth = (get("READCUE_INSECURE_NO_AUTH") or "").lower() in TRUE
        cfg.log_level = cfg.log_level.upper()
        if cfg.log_level not in LOG_LEVELS:
            raise ConfigError(f"READCUE_LOG_LEVEL must be one of {LOG_LEVELS}, got {cfg.log_level!r}")
        cfg.provider = cfg.provider.lower()
        if cfg.provider not in PROVIDERS:
            raise ConfigError(f"READCUE_LLM_PROVIDER must be one of {PROVIDERS}, got {cfg.provider!r}")
        if not -2 <= cfg.pushover_priority <= 1:
            raise ConfigError("PUSHOVER_PRIORITY must be between -2 and 1")
        if cfg.notify_days_before < 0:
            raise ConfigError("READCUE_NOTIFY_DAYS_BEFORE can't be negative")
        if cfg.summarize_days_before < 0:
            raise ConfigError("READCUE_SUMMARIZE_DAYS_BEFORE can't be negative")
        if not re.fullmatch(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*", cfg.ocr_lang):
            raise ConfigError(f"READCUE_OCR_LANG must look like eng or eng+spa, got {cfg.ocr_lang!r}")
        if cfg.max_upload_mb < 1:
            raise ConfigError("READCUE_MAX_UPLOAD_MB must be at least 1")
        if cfg.check_interval_seconds < 1:
            raise ConfigError("READCUE_CHECK_INTERVAL must be at least 1 second")
        return cfg


def is_loopback(address: str) -> bool:
    if address in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def check_exposure(cfg: Config, listen_host: str) -> None:
    """Refuse to serve a network-reachable app that has no password.

    The app can read your textbooks and spend your API key, so "open to the LAN, no login" must be a
    deliberate choice (READCUE_INSECURE_NO_AUTH=true). In Docker the address that matters is the one the
    port is published on (READCUE_BIND, passed in by docker-compose.yml), not the container's own 0.0.0.0.
    """
    exposed_on = cfg.bind or listen_host
    if not cfg.password and not cfg.insecure_no_auth and not is_loopback(exposed_on):
        raise ConfigError(
            f"The app would be reachable on {exposed_on} with no login. Set READCUE_PASSWORD, keep "
            "READCUE_BIND=127.0.0.1, or set READCUE_INSECURE_NO_AUTH=true if you really want that."
        )


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing values (local dev only)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
