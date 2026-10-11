from __future__ import annotations

import ipaddress

from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_networks(value: str) -> list[ipaddress._BaseNetwork]:
    result = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        try:
            result.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"Invalid IP/CIDR in configuration: {item}"
            ) from exc
    return result


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret: str
    admin_username: str = "admin"
    admin_password: str
    remnawave_url: str
    remnawave_api_token: str
    remnawave_caddy_token: str | None = None
    dpi_api_key: str | None = None
    dpi_api_url: str = "https://dpichecker.st/api/v1"
    database_path: str = "/data/remnadown.db"
    poll_interval_seconds: int = 60
    default_drop_percent: float = 45.0
    baseline_samples: int = 12
    confirm_samples: int = 2
    recovery_samples: int = 2
    history_days: int = 30
    panel_timezone: str = "Europe/Moscow"
    ip_allowlist: str = ""
    trusted_proxy_ips: str = "127.0.0.1,::1"
    secure_cookies: bool = True
    webhook_allow_private_ips: bool = False
    webhook_timeout_seconds: float = 10.0
    remnadown_update_command: str = "/opt/remnadown/update.sh"
    domain: str = ""

    @property
    def allowed_networks(self) -> list[ipaddress._BaseNetwork]:
        return parse_networks(self.ip_allowlist)

    @property
    def trusted_proxies(self) -> list[ipaddress._BaseNetwork]:
        return parse_networks(self.trusted_proxy_ips)
