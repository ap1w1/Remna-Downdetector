from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class NodeSettingsUpdate(BaseModel):
    drop_percent: float
    webhooks_enabled: bool
    auto_dpi: bool
    auto_dns_replace: bool = False
    cpu_threshold: float = 0
    ram_threshold: float = 0
    rx_threshold_mbps: float = 0
    tx_threshold_mbps: float = 0
    retention_days: int
    poll_interval_seconds: int | None = None
    dpi_locations: list[str] = Field(default_factory=lambda: ["russia"])
    dpi_pop_ids: list[int] = Field(default_factory=list)
    webhook_ids: list[int] = Field(default_factory=list)
    vless_mode: str = "manual"
    vless_key: str = ""
    vless_username: str = ""
    action_chain: list[dict[str, Any]] = Field(default_factory=list)
    dpi_squad_uuids: list[str] = Field(default_factory=list)
    csrf_token: str


class IpPoolCreate(BaseModel):
    address: str
    priority: int = 100
    csrf_token: str


class IpPoolUpdate(BaseModel):
    priority: int
    csrf_token: str


class IpPoolReorder(BaseModel):
    ids: list[int]
    csrf_token: str


class IpRotationUpdate(BaseModel):
    enabled: bool
    max_replacements: int = 1
    csrf_token: str


class IpActivate(BaseModel):
    update_domains: bool = False
    csrf_token: str


class AppSettingsUpdate(BaseModel):
    history_days: int
    poll_interval_seconds: int = 60
    timezone: str = "Europe/Moscow"
    remnawave_api_version: str = "auto"
    dpi_schedule_enabled: bool = False
    dpi_schedule_mode: str = "interval"
    dpi_schedule_time: str = "03:00"
    dpi_interval_minutes: int = 360
    dpi_schedule_nodes: list[str] = Field(default_factory=list)
    dpi_schedule_locations: list[str] = Field(
        default_factory=lambda: ["russia"]
    )
    dpi_schedule_pop_ids: list[int] = Field(default_factory=list)
    dpi_weekly_schedule: list[dict[str, Any]] = Field(default_factory=list)
    dpi_api_key: str = ""
    dpi_infra_billing_enabled: bool = False
    dpi_balance_threshold: float = 1.0
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_events_topic: str = ""
    telegram_auth_topic: str = ""
    telegram_event_types: list[str] = Field(default_factory=list)
    regru_username: str = ""
    regru_password: str = ""
    cloudflare_api_token: str = ""
    regru_ttl: int = 600
    cloudflare_ttl: int = 600
    csrf_token: str


class WebhookCreate(BaseModel):
    name: str
    url: str
    secret: str = ""
    events: list[str] = Field(default_factory=list)
    csrf_token: str


class WebhookEventsUpdate(BaseModel):
    events: list[str] = Field(default_factory=list)
    enabled: bool = True
    csrf_token: str


class DpiRequest(BaseModel):
    locations: list[str] = Field(default_factory=lambda: ["russia"])
    pop_ids: list[int] = Field(default_factory=list)
    kind: str = "ip"
    csrf_token: str
