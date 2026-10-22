"""Runtime configuration, read from environment variables.

PETTRACK_MODE=local   laptop instance (serial scanner, optional cloud sync)
PETTRACK_MODE=cloud   hosted instance (login required, sync API for the laptop)
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    mode: str = "local"
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 5000
    admin_password: str = ""
    admin_password_hash: str = ""
    require_login: bool = False
    secret_key_env: str = ""
    cloud_url: str = ""
    sync_key: str = ""
    sync_interval: int = 30
    cloud_timeout: float = 3.0
    serial_enabled: bool = True
    serial_port: str = "auto"
    serial_baud: int = 9600
    public_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        mode = (e("PETTRACK_MODE") or "local").strip().lower()
        cfg = cls(
            mode=mode,
            data_dir=Path(e("PETTRACK_DATA_DIR") or "data").resolve(),
            host=e("PETTRACK_HOST") or "0.0.0.0",
            port=int(e("PORT") or e("PETTRACK_HTTP_PORT") or 5000),
            admin_password=e("PETTRACK_ADMIN_PASSWORD") or "",
            admin_password_hash=e("PETTRACK_ADMIN_PASSWORD_HASH") or "",
            require_login=_flag("PETTRACK_REQUIRE_LOGIN", mode == "cloud"),
            secret_key_env=e("PETTRACK_SECRET_KEY") or "",
            cloud_url=((e("PETTRACK_CLOUD_URL") or "").rstrip("/") if mode == "local" else ""),
            sync_key=e("PETTRACK_SYNC_KEY") or e("PETTRACK_CLOUD_KEY") or "",
            sync_interval=int(e("PETTRACK_SYNC_INTERVAL") or 30),
            cloud_timeout=float(e("PETTRACK_CLOUD_TIMEOUT") or 3),
            serial_enabled=_flag("PETTRACK_SERIAL", mode == "local"),
            # PETTRACK_PORT stays the serial port (COM8, /dev/ttyUSB0) like the old app
            serial_port=e("PETTRACK_PORT") or "auto",
            serial_baud=int(e("PETTRACK_BAUD") or 9600),
            public_url=(e("PETTRACK_PUBLIC_URL") or "").rstrip("/"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in {"local", "cloud"}:
            raise ConfigError("PETTRACK_MODE must be 'local' or 'cloud'")
        if self.require_login and not (self.admin_password or self.admin_password_hash):
            raise ConfigError("Login is required: set PETTRACK_ADMIN_PASSWORD (or _HASH)")
        if self.mode == "cloud" and not self.sync_key:
            raise ConfigError("Cloud mode needs PETTRACK_SYNC_KEY (shared with the laptop)")
        if self.cloud_url and not self.sync_key:
            raise ConfigError("PETTRACK_CLOUD_URL is set but PETTRACK_SYNC_KEY is missing")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pettrack.db"

    @property
    def cookie_secure(self) -> bool:
        return self.mode == "cloud"

    def secret_key(self) -> str:
        if self.secret_key_env:
            return self.secret_key_env
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "secret.key"
        if path.exists():
            return path.read_text().strip()
        key = secrets.token_hex(32)
        path.write_text(key)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key
