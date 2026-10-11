from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import re
import secrets
import socket
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from remnadown.clients.dpi import DpiClient
from remnadown.clients.remnawave import RemnawaveClient
from remnadown.config import Settings
from remnadown.icon_cache import INTEGRATION_ICON_URLS, IconCache
from remnadown.models import (
    AppSettingsUpdate,
    DpiRequest,
    IpActivate,
    IpPoolCreate,
    IpPoolReorder,
    IpPoolUpdate,
    IpRotationUpdate,
    NodeSettingsUpdate,
    WebhookCreate,
    WebhookEventsUpdate,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware


ROOT = Path(__file__).parent
logger = logging.getLogger("remnadown")


class DatabaseLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            details = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
            with closing(connect_db()) as db:
                db.execute("INSERT INTO app_logs(level,source,message,details,created_at) VALUES(?,?,?,?,?)", (record.levelname, record.name, record.getMessage()[:2000], details[:12000], utc_now()))
                db.commit()
        except Exception:
            pass


settings = Settings()  # type: ignore[call-arg]
templates = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
security = HTTPBasic(auto_error=False)
login_attempts: dict[str, deque[float]] = defaultdict(deque)
ip_rotation_tasks: set[str] = set()
workflow_tasks: set[str] = set()
icon_cache = IconCache(
    Path(settings.database_path).expanduser().resolve().parent / "icon-cache",
    logger,
)
permission_alerts: deque[dict[str, str]] = deque(maxlen=20)
managed_domains_cache: dict[str, Any] | None = None
managed_domains_cache_at = 0.0
managed_domains_cache_lock = asyncio.Lock()
MANAGED_DOMAINS_CACHE_SECONDS = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def integration_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        if not response.is_stream_consumed:
            return f"HTTP {response.status_code}: response body was not read"
        try:
            payload = response.json()
            message = payload.get("message") or payload.get("error") or payload.get("errors")
            if message:
                return f"HTTP {response.status_code}: {message}"
        except ValueError:
            pass
        return f"HTTP {response.status_code}: {response.text[:500]}"
    return str(exc)


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_db()) as db:
        db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        migrations = {
            "nodes": [("country_code", "TEXT NOT NULL DEFAULT ''"), ("provider_name", "TEXT NOT NULL DEFAULT ''"), ("provider_icon", "TEXT NOT NULL DEFAULT ''"), ("cpu_count", "INTEGER"), ("cpu_model", "TEXT NOT NULL DEFAULT ''"), ("total_ram", "INTEGER"), ("node_version", "TEXT NOT NULL DEFAULT ''"), ("xray_version", "TEXT NOT NULL DEFAULT ''"), ("uptime", "TEXT NOT NULL DEFAULT ''"), ("view_position", "INTEGER NOT NULL DEFAULT 0")],
            "samples": [("cpu_percent", "REAL"), ("ram_percent", "REAL"), ("load_1", "REAL"), ("load_5", "REAL"), ("load_15", "REAL"), ("rx_bps", "REAL"), ("tx_bps", "REAL"), ("rx_total", "INTEGER"), ("tx_total", "INTEGER")],
            "node_settings": [("retention_days", "INTEGER"), ("poll_interval_seconds", "INTEGER"), ("dpi_locations", "TEXT NOT NULL DEFAULT '[\"russia\"]'"), ("dpi_pop_ids", "TEXT NOT NULL DEFAULT '[]'"), ("auto_ip_replace", "INTEGER NOT NULL DEFAULT 0"), ("max_ip_replacements", "INTEGER NOT NULL DEFAULT 1"), ("ip_replacements", "INTEGER NOT NULL DEFAULT 0"), ("auto_dns_replace", "INTEGER NOT NULL DEFAULT 0"), ("cpu_threshold", "REAL NOT NULL DEFAULT 0"), ("ram_threshold", "REAL NOT NULL DEFAULT 0"), ("rx_threshold_mbps", "REAL NOT NULL DEFAULT 0"), ("tx_threshold_mbps", "REAL NOT NULL DEFAULT 0"), ("vless_mode", "TEXT NOT NULL DEFAULT 'manual'"), ("vless_key", "TEXT NOT NULL DEFAULT ''"), ("vless_username", "TEXT NOT NULL DEFAULT ''"), ("action_chain", "TEXT NOT NULL DEFAULT '[]'")],
            "dpi_checks": [
                ("provider_id", "INTEGER"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("location", "TEXT NOT NULL DEFAULT 'russia'"),
                ("billing_recorded", "INTEGER NOT NULL DEFAULT 0"),
                ("automatic", "INTEGER NOT NULL DEFAULT 0"),
                ("kind", "TEXT NOT NULL DEFAULT 'ip'"),
                ("source", "TEXT NOT NULL DEFAULT 'manual'"),
                ("temp_user_uuid", "TEXT NOT NULL DEFAULT ''"),
                ("context", "TEXT NOT NULL DEFAULT '{}'"),
            ],
            "node_state": [("is_unavailable", "INTEGER NOT NULL DEFAULT 0"), ("cpu_alert", "INTEGER NOT NULL DEFAULT 0"), ("ram_alert", "INTEGER NOT NULL DEFAULT 0"), ("rx_alert", "INTEGER NOT NULL DEFAULT 0"), ("tx_alert", "INTEGER NOT NULL DEFAULT 0")],
        }
        migrations["node_settings"].append(("dpi_squad_uuids", "TEXT NOT NULL DEFAULT '[]'"))
        for table, columns in migrations.items():
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns:
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        db.commit()


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "0.0.0.0"
    try:
        trusted = any(ipaddress.ip_address(peer) in network for network in settings.trusted_proxies)
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            try:
                ipaddress.ip_address(forwarded)
                return forwarded
            except ValueError:
                pass
    return peer


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if settings.allowed_networks:
            try:
                allowed = any(ipaddress.ip_address(client_ip(request)) in item for item in settings.allowed_networks)
            except ValueError:
                allowed = False
            if not allowed:
                return JSONResponse({"detail": "IP is not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            "Cache-Control": "no-store",
        })
        return response


def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(user)


def verify_csrf(request: Request, token: str) -> None:
    expected = request.session.get("csrf")
    if not expected or not hmac.compare_digest(str(expected), token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def render(request: Request, name: str, **context: Any) -> HTMLResponse:
    template = templates.get_template(name)
    context.update(request=request, csrf=request.session.get("csrf", ""))
    return HTMLResponse(template.render(**context))


def create_remnawave_client() -> RemnawaveClient:
    return RemnawaveClient(settings, logger, permission_alerts)


def node_value(node: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in node and node[name] is not None:
            return node[name]
    return default


def number_or_none(value: Any) -> float | None:
    try:
        if isinstance(value, dict):
            value = value.get("percent", value.get("usage"))
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_bytes(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).strip().upper().replace("IB", "B")
        for suffix, multiplier in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
            if text.endswith(suffix):
                try:
                    return int(float(text[:-2].strip()) * multiplier)
                except ValueError:
                    return None
    return None


def traffic_value(value: Any) -> tuple[int, bool]:
    """Return bytes and whether the source has byte-level precision."""
    if value is None:
        return 0, False
    text = str(value).strip()
    exact = text.lstrip("+-").isdigit()
    return parse_bytes(value) or 0, exact


def normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    provider = node_value(node, "provider", default={}) or {}
    system = node_value(node, "system", "systemInfo", "metrics", default={}) or {}
    system_info = node_value(system, "info", default={}) or {}
    system_stats = node_value(system, "stats", default={}) or {}
    interface = node_value(system_stats, "interface", default={}) or {}
    versions = node_value(node, "versions", default={}) or {}
    memory = node_value(system, "memory", default={}) or {}
    load = node_value(system_stats, "loadAvg", default=node_value(system, "loadAverage", "load", default=[])) or []
    if isinstance(load, dict):
        load = [load.get("one", load.get("load1")), load.get("five", load.get("load5")), load.get("fifteen", load.get("load15"))]
    return {
        "uuid": str(node_value(node, "uuid", "id")),
        "name": str(node_value(node, "name", default="Unnamed node")),
        "address": str(node_value(node, "address", "host", default="")),
        "country_code": str(node_value(node, "countryCode", "country_code", default="")).upper()[:2],
        "online": int(node_value(node, "usersOnline", "onlineUsers", "online", "activeUsers", default=0) or 0),
        "connected": bool(node_value(node, "isConnected", "isOnline", default=True)),
        "provider_name": str(node_value(provider, "name", default=node_value(node, "providerName", default="")) or ""),
        "provider_icon": str(node_value(provider, "faviconLink", default="") or ""),
        "cpu_count": node_value(system_info, "cpus", default=node_value(node, "cpuCount")), "cpu_model": str(node_value(system_info, "cpuModel", default=node_value(node, "cpuModel", default="")) or ""),
        "total_ram": parse_bytes(node_value(system_info, "memoryTotal", default=node_value(node, "totalRam"))), "node_version": str(node_value(versions, "node", default=node_value(node, "nodeVersion", default="")) or ""),
        "xray_version": str(node_value(versions, "xray", default=node_value(node, "xrayVersion", default="")) or ""), "uptime": str(node_value(node, "xrayUptime", default="") or ""),
        "view_position": int(node_value(node, "viewPosition", default=0) or 0),
        "cpu_percent": number_or_none(node_value(system_stats, "cpuPercent", "cpuUsage", default=node_value(system, "cpuPercent", "cpuUsage", "cpu"))),
        "ram_percent": number_or_none(node_value(memory, "percent", "usagePercent", default=node_value(system, "ramPercent", "memoryPercent"))),
        "memory_used": parse_bytes(node_value(system_stats, "memoryUsed")), "memory_total": parse_bytes(node_value(system_info, "memoryTotal")),
        "rx_bps": number_or_none(node_value(interface, "rxBytesPerSec")), "tx_bps": number_or_none(node_value(interface, "txBytesPerSec")),
        "rx_total": parse_bytes(node_value(interface, "rxTotal")), "tx_total": parse_bytes(node_value(interface, "txTotal")),
        "load": [number_or_none(value) for value in list(load)[:3]],
        "raw": node,
    }


def public_webhook_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if settings.webhook_allow_private_ips:
        return True
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return False
    return True


def dpi_callback_token() -> str:
    return hmac.new(settings.app_secret.encode(), b"dpi-callback", hashlib.sha256).hexdigest()[:40]


def dpi_callback_url() -> str:
    domain = settings.domain.strip().rstrip("/")
    if not domain or domain in {"localhost", "monitor.example.com"}:
        return ""
    base = domain if domain.startswith("https://") else f"https://{domain}"
    return f"{base}/api/dpi/callback/{dpi_callback_token()}"


def create_dpi_client() -> DpiClient:
    token = settings.dpi_api_key or ""
    try:
        with closing(connect_db()) as db:
            token = setting_value(db, "dpi_api_key", token)
    except sqlite3.Error:
        pass
    return DpiClient(settings.dpi_api_url, token, dpi_callback_url())


async def send_webhooks(event: str, node: dict[str, Any], sample: dict[str, Any]) -> None:
    with closing(connect_db()) as db:
        selected = db.execute("SELECT webhook_id FROM node_webhooks WHERE node_uuid=?", (node["uuid"],)).fetchall()
        if selected:
            hooks = db.execute("SELECT w.* FROM webhooks w JOIN node_webhooks nw ON nw.webhook_id=w.id WHERE nw.node_uuid=? AND w.enabled=1", (node["uuid"],)).fetchall()
        else:
            hooks = db.execute("SELECT * FROM webhooks WHERE enabled=1").fetchall()
        hooks = [hook for hook in hooks if not db.execute("SELECT 1 FROM webhook_events WHERE webhook_id=? LIMIT 1", (hook["id"],)).fetchone() or db.execute("SELECT 1 FROM webhook_events WHERE webhook_id=? AND event=?", (hook["id"], event)).fetchone()]
    payload = {"event": event, "node": node, "sample": sample, "timestamp": utc_now(), "source": "RemnaDownDetector"}
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, follow_redirects=False) as client:
        for hook in hooks:
            if not public_webhook_url(hook["url"]):
                logger.warning("Rejected unsafe webhook URL: %s", hook["url"])
                continue
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers = {"Content-Type": "application/json", "User-Agent": "RemnaDownDetector/1.0"}
            if hook["secret"]:
                headers["X-Remna-Signature"] = "sha256=" + hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
            try:
                await client.post(hook["url"], content=body, headers=headers)
            except httpx.HTTPError:
                logger.exception("Webhook delivery failed")


async def send_global_webhooks(event: str, data: dict[str, Any]) -> None:
    with closing(connect_db()) as db:
        hooks = db.execute("SELECT * FROM webhooks WHERE enabled=1").fetchall()
        hooks = [hook for hook in hooks if not db.execute("SELECT 1 FROM webhook_events WHERE webhook_id=? LIMIT 1", (hook["id"],)).fetchone() or db.execute("SELECT 1 FROM webhook_events WHERE webhook_id=? AND event=?", (hook["id"], event)).fetchone()]
    payload = {"event": event, "data": data, "timestamp": utc_now(), "source": "RemnaDownDetector"}
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, follow_redirects=False) as client:
        for hook in hooks:
            if not public_webhook_url(hook["url"]):
                continue
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers = {"Content-Type": "application/json", "User-Agent": "RemnaDownDetector/1.0"}
            if hook["secret"]:
                headers["X-Remna-Signature"] = "sha256=" + hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
            try:
                await client.post(hook["url"], content=body, headers=headers)
            except httpx.HTTPError:
                logger.exception("Global webhook delivery failed")


async def telegram_notify(category: str, message: str, event_type: str = "") -> None:
    with closing(connect_db()) as db:
        token = setting_value(db, "telegram_bot_token", "")
        chat_id = setting_value(db, "telegram_chat_id", "")
        topic = setting_value(db, f"telegram_{category}_topic", "")
        enabled = setting_value(db, "telegram_enabled", "false") == "true"
        selected_events = json.loads(setting_value(db, "telegram_event_types", "[]"))
    if not enabled or not token or not chat_id:
        return
    if event_type and selected_events and event_type not in selected_events:
        return
    payload: dict[str, Any] = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    if topic:
        payload["message_thread_id"] = int(topic)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            if response.status_code >= 400:
                logger.warning("Telegram notification failed with HTTP %s", response.status_code)
    except httpx.HTTPError:
        logger.warning("Telegram notification transport failed")


def event_message(event: str, node: dict[str, Any], sample: dict[str, Any]) -> str:
    labels = {"node.unavailable": "🔴 Нода недоступна", "node.available": "🟢 Нода снова доступна", "node.online_drop": "🟠 Падение онлайна", "node.online_recovered": "🟢 Онлайн восстановлен"}
    return f"<b>{labels.get(event, event)}</b>\nНода: <b>{html.escape(str(node['name']))}</b>\nАдрес: <code>{html.escape(str(node.get('address', '')))}</code>\nОнлайн: {int(sample.get('online', 0))}"


def node_config(db: sqlite3.Connection, uuid: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM node_settings WHERE node_uuid=?", (uuid,)).fetchone()


async def process_node(node: dict[str, Any]) -> None:
    now = utc_now()
    event: str | None = None
    metric = node.get("metric", {})
    inbound = metric.get("inboundsStats", []) if isinstance(metric, dict) else []
    rx_values = [traffic_value(item.get("download")) for item in inbound]
    tx_values = [traffic_value(item.get("upload")) for item in inbound]
    rx_total = node.get("rx_total") if node.get("rx_total") is not None else sum(value for value, _ in rx_values)
    tx_total = node.get("tx_total") if node.get("tx_total") is not None else sum(value for value, _ in tx_values)
    traffic_exact = bool(inbound) and all(exact for _, exact in rx_values + tx_values)
    load = (node.get("load") or []) + [None, None, None]
    cpu_percent = node.get("cpu_percent")
    if cpu_percent is None and load[0] is not None and node.get("cpu_count"):
        cpu_percent = min(100.0, max(0.0, float(load[0]) / int(node["cpu_count"]) * 100))
    ram_percent = node.get("ram_percent")
    if ram_percent is None and node.get("memory_used") is not None and node.get("memory_total"):
        ram_percent = min(100.0, max(0.0, int(node["memory_used"]) / int(node["memory_total"]) * 100))
    sample: dict[str, Any] = {"online": node["online"], "connected": node["connected"], "cpu_percent": cpu_percent, "ram_percent": ram_percent, "rx_total": rx_total, "tx_total": tx_total}
    resource_events: list[str] = []
    with closing(connect_db()) as db:
        provider_name = node.get("provider_name") or metric.get("providerName", "")
        db.execute("INSERT INTO nodes(uuid,name,address,country_code,provider_name,provider_icon,cpu_count,cpu_model,total_ram,node_version,xray_version,uptime,view_position,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET name=excluded.name,address=excluded.address,country_code=excluded.country_code,provider_name=excluded.provider_name,provider_icon=excluded.provider_icon,cpu_count=excluded.cpu_count,cpu_model=excluded.cpu_model,total_ram=excluded.total_ram,node_version=excluded.node_version,xray_version=excluded.xray_version,uptime=excluded.uptime,view_position=excluded.view_position,last_seen=excluded.last_seen", (node["uuid"], node["name"], node["address"], node["country_code"], provider_name, node.get("provider_icon", ""), node.get("cpu_count"), node.get("cpu_model", ""), node.get("total_ram"), node.get("node_version", ""), node.get("xray_version", ""), node.get("uptime", ""), node.get("view_position", 0), now))
        db.execute("INSERT OR IGNORE INTO node_settings(node_uuid,vless_mode) VALUES(?,'temporary')", (node["uuid"],))
        previous_metric = db.execute("SELECT rx_total,tx_total,created_at FROM samples WHERE node_uuid=? ORDER BY id DESC LIMIT 1", (node["uuid"],)).fetchone()
        rx_bps, tx_bps = node.get("rx_bps"), node.get("tx_bps")
        if rx_bps is None and tx_bps is None and traffic_exact and previous_metric and previous_metric["rx_total"] is not None:
            elapsed = max(1, (datetime.fromisoformat(now) - datetime.fromisoformat(previous_metric["created_at"])).total_seconds())
            rx_bps = max(0, rx_total - int(previous_metric["rx_total"])) / elapsed
            tx_bps = max(0, tx_total - int(previous_metric["tx_total"])) / elapsed
        sample.update({"rx_bps": rx_bps, "tx_bps": tx_bps})
        db.execute("INSERT INTO samples(node_uuid,online,connected,cpu_percent,ram_percent,load_1,load_5,load_15,rx_bps,tx_bps,rx_total,tx_total,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (node["uuid"], node["online"], int(node["connected"]), cpu_percent, ram_percent, load[0], load[1], load[2], rx_bps, tx_bps, rx_total, tx_total, now))
        config = node_config(db, node["uuid"])
        threshold = float(config["drop_percent"] if config and config["drop_percent"] is not None else settings.default_drop_percent)
        rows = db.execute("SELECT online FROM samples WHERE node_uuid=? ORDER BY id DESC LIMIT ?", (node["uuid"], settings.baseline_samples + 1)).fetchall()
        previous = [row["online"] for row in rows[1:] if row["online"] >= 0]
        baseline = sorted(previous)[len(previous) // 2] if previous else node["online"]
        drop = 0.0 if not node["connected"] else (max(0.0, (baseline - node["online"]) / baseline * 100) if baseline else 0.0)
        sample.update({"baseline": baseline, "drop_percent": round(drop, 2)})
        state = db.execute("SELECT * FROM node_state WHERE node_uuid=?", (node["uuid"],)).fetchone()
        is_down = bool(state["is_down"]) if state else False
        was_unavailable = bool(state["is_unavailable"]) if state else False
        is_unavailable = not node["connected"]
        if is_unavailable:
            bad_count, good_count = (state["bad_count"] if state else 0), 0
        else:
            bad_count = (state["bad_count"] if state else 0) + 1 if drop >= threshold else 0
            good_count = (state["good_count"] if state else 0) + 1 if drop < threshold else 0
        if is_unavailable and not was_unavailable:
            event = "node.unavailable"
        elif not is_unavailable and was_unavailable:
            event = "node.available"
        elif not is_unavailable and not is_down and bad_count >= settings.confirm_samples:
            is_down, event = True, "node.online_drop"
        elif not is_unavailable and is_down and good_count >= settings.recovery_samples:
            is_down, event = False, "node.online_recovered"
        checks = [("cpu", cpu_percent, float(config["cpu_threshold"] or 0) if config else 0), ("ram", ram_percent, float(config["ram_threshold"] or 0) if config else 0), ("rx", (rx_bps or 0) * 8 / 1_000_000, float(config["rx_threshold_mbps"] or 0) if config else 0), ("tx", (tx_bps or 0) * 8 / 1_000_000, float(config["tx_threshold_mbps"] or 0) if config else 0)]
        alerts: dict[str, int] = {}
        for name, value, limit in checks:
            was_alert = bool(state[f"{name}_alert"]) if state else False
            active = bool(limit > 0 and value is not None and value >= limit)
            alerts[name] = int(active)
            if active and not was_alert:
                resource_events.append(f"node.{name}_threshold")
        db.execute("INSERT INTO node_state(node_uuid,is_down,bad_count,good_count,is_unavailable,cpu_alert,ram_alert,rx_alert,tx_alert,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(node_uuid) DO UPDATE SET is_down=excluded.is_down,bad_count=excluded.bad_count,good_count=excluded.good_count,is_unavailable=excluded.is_unavailable,cpu_alert=excluded.cpu_alert,ram_alert=excluded.ram_alert,rx_alert=excluded.rx_alert,tx_alert=excluded.tx_alert,changed_at=CASE WHEN is_down!=excluded.is_down OR is_unavailable!=excluded.is_unavailable THEN excluded.changed_at ELSE changed_at END", (node["uuid"], int(is_down), bad_count, good_count, int(is_unavailable), alerts["cpu"], alerts["ram"], alerts["rx"], alerts["tx"], now))
        if event:
            db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (node["uuid"], event, json.dumps(sample), now))
        for resource_event in resource_events:
            db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (node["uuid"], resource_event, json.dumps({**sample, "rx_mbps": (rx_bps or 0) * 8 / 1_000_000, "tx_mbps": (tx_bps or 0) * 8 / 1_000_000}), now))
        db.commit()
        webhook_enabled = not config or bool(config["webhooks_enabled"])
        auto_dpi = bool(config and config["auto_dpi"])
        auto_dpi_pop_ids = json.loads(config["dpi_pop_ids"] or "[]") if config else []
    icon_cache.schedule(
        str(node.get("provider_icon", "")),
        is_allowed=public_webhook_url,
    )
    if event and webhook_enabled:
        await send_webhooks(event, node, sample)
    if event:
        await telegram_notify("events", event_message(event, node, sample), event)
    for resource_event in resource_events:
        if webhook_enabled:
            await send_webhooks(resource_event, node, sample)
        await telegram_notify("events", event_message(resource_event, node, sample), resource_event)
    if event == "node.online_drop":
        chain = json.loads(config["action_chain"] or "[]") if config and config["action_chain"] else []
        if not chain and auto_dpi:
            chain = [{"type": "dpi_ip", "enabled": True}]
        if chain and node["uuid"] not in workflow_tasks:
            workflow_tasks.add(node["uuid"])
            asyncio.create_task(run_action_chain(node, sample, chain, auto_dpi_pop_ids or None))


def save_dpi(
    node_uuid: str,
    target: str,
    result: dict[str, Any],
    location: str = "russia",
    automatic: bool = False,
    kind: str = "ip",
    source: str = "manual",
    temp_user_uuid: str = "",
    context: dict[str, Any] | None = None,
) -> int:
    with closing(connect_db()) as db:
        now = utc_now()
        cursor = db.execute(
            "INSERT INTO dpi_checks(node_uuid,target,location,provider_id,"
            "status,result,created_at,updated_at,automatic,kind,source,"
            "temp_user_uuid,context) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                node_uuid,
                target,
                location,
                result.get("check_id") or result.get("id"),
                str(result.get("status", "unknown")),
                json.dumps(result),
                now,
                now,
                int(automatic),
                kind,
                source,
                temp_user_uuid,
                json.dumps(context or {}),
            ),
        )
        db.commit()
        return int(cursor.lastrowid)


async def record_dpi_billing(row_id: int | None, result: dict[str, Any]) -> None:
    with closing(connect_db()) as db:
        enabled = setting_value(db, "dpi_infra_billing_enabled", "false") == "true"
        row = db.execute("SELECT billing_recorded,created_at,provider_id FROM dpi_checks WHERE id=?", (row_id,)).fetchone() if row_id else None
    amount = number_or_none(result.get("usd_cost", result.get("cost", result.get("estimated_cost"))))
    provider_check_id = result.get("id") or result.get("check_id") or (row["provider_id"] if row else None)
    if not enabled or not provider_check_id or amount is None or amount <= 0:
        return
    billed_at = result.get("completed_at") or result.get("created_at") or (row["created_at"] if row else utc_now())
    with closing(connect_db()) as db:
        if row and row["billing_recorded"]:
            db.execute("INSERT OR IGNORE INTO dpi_billing_sync(provider_check_id,amount,billed_at,status,error,updated_at) VALUES(?,?,?,'recorded','',?)", (provider_check_id, amount, billed_at, utc_now()))
            db.commit()
            return
        known = db.execute("SELECT status FROM dpi_billing_sync WHERE provider_check_id=?", (provider_check_id,)).fetchone()
        if known and known["status"] in {"pending", "recorded"}:
            return
        db.execute("INSERT INTO dpi_billing_sync(provider_check_id,amount,billed_at,status,error,updated_at) VALUES(?,?,?,'pending','',?) ON CONFLICT(provider_check_id) DO UPDATE SET amount=excluded.amount,billed_at=excluded.billed_at,status='pending',error='',updated_at=excluded.updated_at", (provider_check_id, amount, billed_at, utc_now()))
        db.commit()
    client = create_remnawave_client()
    try:
        providers = await client.infra_providers()
        provider = next((item for item in providers if str(item.get("name", "")).upper() == "DPI//CHECKER"), None)
        if provider is None:
            provider = await client.create_infra_provider("DPI//CHECKER")
        provider_uuid = provider.get("uuid")
        if not provider_uuid:
            raise ValueError("Remnawave did not return the infra provider UUID")
        await client.create_billing_record(str(provider_uuid), float(amount), billed_at)
        with closing(connect_db()) as db:
            db.execute("UPDATE dpi_billing_sync SET status='recorded',updated_at=? WHERE provider_check_id=?", (utc_now(), provider_check_id))
            if row_id:
                db.execute("UPDATE dpi_checks SET billing_recorded=1 WHERE id=?", (row_id,))
            db.commit()
    except Exception as exc:
        with closing(connect_db()) as db:
            db.execute("UPDATE dpi_billing_sync SET status='failed',error=?,updated_at=? WHERE provider_check_id=?", (str(exc)[:500], utc_now(), provider_check_id))
            db.commit()
        logger.exception("Failed to write DPI cost to Remnawave infra billing")
    finally:
        await client.close()


async def sync_dpi_billing(checks: list[dict[str, Any]]) -> None:
    for check in checks:
        await record_dpi_billing(None, check)


async def queue_dpi(
    node_uuid: str,
    target: str,
    location: str,
    pop_ids: list[int] | None = None,
    automatic: bool = False,
    kind: str = "ip",
    source: str = "manual",
    temp_user_uuid: str = "",
    context: dict[str, Any] | None = None,
) -> int:
    try:
        dpi_client = create_dpi_client()
        result = await (
            dpi_client.start_vpn(target, location, pop_ids)
            if kind == "vless"
            else dpi_client.start_ip(target, location, pop_ids)
        )
        row_id = save_dpi(
            node_uuid,
            target,
            result,
            location,
            automatic,
            kind,
            source,
            temp_user_uuid,
            context,
        )
        public_target = "[VLESS hidden]" if kind == "vless" else target
        event_payload = {
            "target": public_target,
            "kind": kind,
            "source": source,
            "location": location,
            "provider_id": result.get("id") or result.get("check_id"),
            "temp_user_uuid": temp_user_uuid,
            **(context or {}),
        }
        with closing(connect_db()) as db:
            db.execute(
                "INSERT INTO events(node_uuid,event,payload,created_at) "
                "VALUES(?,?,?,?)",
                (
                    node_uuid,
                    "dpi.check_started",
                    json.dumps(event_payload),
                    utc_now(),
                ),
            )
            node = db.execute(
                "SELECT * FROM nodes WHERE uuid=?",
                (node_uuid,),
            ).fetchone()
            db.commit()
        logger.info(
            "DPI %s check started node=%s source=%s provider_id=%s "
            "temp_user=%s context=%s",
            kind,
            node_uuid,
            source,
            result.get("id") or result.get("check_id"),
            temp_user_uuid or "none",
            context or {},
        )
        if node:
            await send_webhooks("dpi.check_started", dict(node), event_payload)
            await telegram_notify(
                "events",
                f"<b>DPI-проверка запущена</b>\n"
                f"Нода: <b>{html.escape(node['name'])}</b>\n"
                f"Цель: <code>{html.escape(public_target)}</code>\n"
                f"Тип: {kind}",
                "dpi.check_started",
            )
        if number_or_none(result.get("estimated_cost", result.get("usd_cost"))):
            await record_dpi_billing(row_id, result)
        provider_id = result.get("check_id") or result.get("id")
        if provider_id:
            asyncio.create_task(follow_dpi_check(row_id, int(provider_id)))
        elif result.get("status") in {"completed", "failed", "cancelled"}:
            await record_dpi_billing(row_id, result)
        return row_id
    except Exception as exc:
        error = integration_error(exc)
        logger.exception(
            "Failed to start DPI check for node %s context=%s: %s",
            node_uuid,
            context or {},
            error,
        )
        row_id = save_dpi(
            node_uuid,
            target,
            {"status": "failed", "error": error},
            location,
            automatic,
            kind,
            source,
            temp_user_uuid,
            context,
        )
        failed_payload = {
            "target": "[VLESS hidden]" if kind == "vless" else target,
            "kind": kind,
            "source": source,
            "status": "failed",
            "error": error,
            **(context or {}),
        }
        with closing(connect_db()) as db:
            db.execute(
                "INSERT INTO events(node_uuid,event,payload,created_at) "
                "VALUES(?,?,?,?)",
                (
                    node_uuid,
                    "dpi.check_completed",
                    json.dumps(failed_payload),
                    utc_now(),
                ),
            )
            db.commit()
        if temp_user_uuid:
            try:
                await app.state.remna.delete_user(temp_user_uuid)
            except Exception:
                logger.exception("Failed to delete temporary DPI user %s", temp_user_uuid)
        return row_id


async def vless_target(
    node_uuid: str,
) -> tuple[str, str, str, dict[str, str]]:
    with closing(connect_db()) as db:
        config = node_config(db, node_uuid)
        node = db.execute(
            "SELECT uuid,name,address,country_code FROM nodes WHERE uuid=?",
            (node_uuid,),
        ).fetchone()
    if not node:
        raise ValueError("Node not found")
    mode = (config["vless_mode"] if config else "temporary") or "temporary"
    if mode == "manual":
        if not config or not config["vless_key"]:
            raise ValueError("VLESS key is not configured for the node")
        return config["vless_key"], "node", "", {}
    if mode == "remnawave":
        if not config or not config["vless_username"]:
            raise ValueError("Remnawave username is not configured")
        user = await app.state.remna.user_by_username(config["vless_username"])
        key = await app.state.remna.vless_key(
            user,
            str(node["name"]),
            str(node["address"]),
        )
        return key, "remnawave", "", {
            "remnawave_username": str(config["vless_username"]),
        }

    key, user, squad = await app.state.remna.temporary_vless(
        node_uuid,
        str(node["name"]),
        str(node["address"]),
        str(node["country_code"]),
    )
    return key, "temporary", str(user.get("uuid", "")), {
        "squad_uuid": str(squad["uuid"]),
        "squad_name": str(squad["name"]),
        "temporary_username": str(user.get("username", "")),
    }


async def run_action_chain(node: dict[str, Any], sample: dict[str, Any], chain: list[dict[str, Any]], pop_ids: list[int] | None) -> None:
    try:
        for index, action in enumerate(chain):
            if not action.get("enabled", True):
                continue
            action_type = action.get("type")
            payload = {"step": index + 1, "action": action_type, "status": "started"}
            with closing(connect_db()) as db:
                db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (node["uuid"], "rule.action_started", json.dumps(payload), utc_now()))
                db.commit()
            await send_webhooks("rule.action_started", node, payload)
            try:
                if action_type == "dpi_ip":
                    await queue_dpi(node["uuid"], node["address"], "russia", pop_ids, True, "ip", "rule")
                elif action_type == "dpi_vless":
                    target, source, temp_user, context = await vless_target(
                        node["uuid"]
                    )
                    await queue_dpi(
                        node["uuid"],
                        target,
                        "russia",
                        pop_ids,
                        automatic=True,
                        kind="vless",
                        source=source,
                        temp_user_uuid=temp_user,
                        context=context,
                    )
                elif action_type == "rotate_ip":
                    await rotate_node_ip(app, node["uuid"])
                elif action_type == "replace_dns":
                    with closing(connect_db()) as db:
                        current = db.execute("SELECT address FROM nodes WHERE uuid=?", (node["uuid"],)).fetchone()
                    if current and current["address"] != node["address"]:
                        await replace_managed_dns(app, node["uuid"], node["address"], current["address"], force=True)
                else:
                    raise ValueError(f"Unknown rule action: {action_type}")
                payload["status"] = "completed"
            except Exception as exc:
                payload.update(status="failed", error=str(exc)[:500])
                logger.exception("Rule action failed node=%s action=%s", node["uuid"], action_type)
            with closing(connect_db()) as db:
                db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (node["uuid"], "rule.action_completed", json.dumps(payload), utc_now()))
                db.commit()
            await send_webhooks("rule.action_completed", node, payload)
    finally:
        workflow_tasks.discard(node["uuid"])


async def follow_dpi_check(row_id: int, provider_id: int) -> None:
    if not provider_id:
        return
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        with closing(connect_db()) as db:
            current = db.execute("SELECT status FROM dpi_checks WHERE id=?", (row_id,)).fetchone()
        if current and current["status"] in {"completed", "failed", "cancelled"}:
            return
        await asyncio.sleep(8)
        try:
            result = await create_dpi_client().wait_check(provider_id)
            with closing(connect_db()) as db:
                db.execute("UPDATE dpi_checks SET status=?,result=?,updated_at=? WHERE id=?", (str(result.get("status", "unknown")), json.dumps(result), utc_now(), row_id))
                db.commit()
            if result.get("status") in {"completed", "failed", "cancelled"}:
                await record_dpi_billing(row_id, result)
                with closing(connect_db()) as db:
                    row = db.execute(
                        "SELECT node_uuid,target,automatic,kind,source,"
                        "temp_user_uuid,context FROM dpi_checks WHERE id=?",
                        (row_id,),
                    ).fetchone()
                    if row:
                        shown_target = "[VLESS hidden]" if row["kind"] == "vless" else row["target"]
                        completed = {
                            **json.loads(row["context"] or "{}"),
                            "target": shown_target,
                            "kind": row["kind"],
                            "source": row["source"],
                            "status": result.get("status"),
                            "results": result.get("results", []),
                        }
                        db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (row["node_uuid"], "dpi.check_completed", json.dumps(completed), utc_now()))
                        db.commit()
                if row:
                    with closing(connect_db()) as event_db:
                        event_node = event_db.execute("SELECT * FROM nodes WHERE uuid=?", (row["node_uuid"],)).fetchone()
                    if event_node:
                        await send_webhooks("dpi.check_completed", dict(event_node), completed)
                    if row["temp_user_uuid"]:
                        try:
                            await app.state.remna.delete_user(row["temp_user_uuid"])
                        except Exception:
                            logger.exception("Failed to delete temporary DPI user %s", row["temp_user_uuid"])
                if row and row["automatic"] and result.get("status") == "completed" and dpi_result_unavailable(result):
                    asyncio.create_task(rotate_node_ip(app, row["node_uuid"]))
                if row:
                    with closing(connect_db()) as db:
                        node = db.execute("SELECT * FROM nodes WHERE uuid=?", (row["node_uuid"],)).fetchone()
                    if node:
                        await telegram_notify("events", f"<b>DPI-проверка завершена</b>\nНода: <b>{html.escape(node['name'])}</b>\nIP: <code>{html.escape(row['target'])}</code>\nСтатус: {html.escape(str(result.get('status', 'unknown')))}", "dpi.check_completed")
                return
            if result.get("timed_out"):
                continue
        except Exception:
            logger.exception("DPI check polling failed for %s", provider_id)
    with closing(connect_db()) as db:
        row = db.execute("SELECT temp_user_uuid FROM dpi_checks WHERE id=?", (row_id,)).fetchone()
        db.execute("UPDATE dpi_checks SET status='timeout',updated_at=? WHERE id=?", (utc_now(), row_id))
        db.commit()
    if row and row["temp_user_uuid"]:
        try:
            await app.state.remna.delete_user(row["temp_user_uuid"])
        except Exception:
            logger.exception("Failed to delete timed-out DPI user %s", row["temp_user_uuid"])


async def check_dpi_balance() -> None:
    if not dpi_is_configured():
        return
    with closing(connect_db()) as db:
        threshold = float(setting_value(db, "dpi_balance_threshold", "1"))
        last_check = setting_value(db, "dpi_balance_checked_at", "")
        if last_check and datetime.fromisoformat(last_check) > datetime.now(timezone.utc) - timedelta(minutes=15):
            return
        db.execute("INSERT INTO app_settings(key,value) VALUES('dpi_balance_checked_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utc_now(),))
        db.commit()
    try:
        profile = await create_dpi_client().profile()
        profile = profile.get("data", profile)
        balance = number_or_none(profile.get("balance", profile.get("usd_balance", profile.get("balance_usd"))))
        if balance is None:
            return
        with closing(connect_db()) as db:
            notified = setting_value(db, "dpi_balance_low_notified", "false") == "true"
            db.execute("INSERT INTO app_settings(key,value) VALUES('dpi_balance_low_notified',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("true" if balance <= threshold else "false",))
            db.commit()
        if balance <= threshold and not notified:
            await send_global_webhooks("dpi.balance_low", {"balance": balance, "threshold": threshold, "currency": "USD"})
    except Exception:
        logger.exception("DPI balance check failed")


async def test_node_address(app: FastAPI, node_uuid: str, address: str, restore_address: str | None) -> tuple[bool, str]:
    try:
        await app.state.remna.update_node_address(node_uuid, address)
        await asyncio.sleep(15)
        raw_nodes = await app.state.remna.nodes()
        raw = next((item for item in raw_nodes if str(node_value(item, "uuid", "id")) == node_uuid), None)
        if raw is None:
            return False, "Нода не найдена после смены IP"
        connected = normalize_node(raw)["connected"]
        return (True, "") if connected else (False, "Нода не подключилась за 15 секунд")
    except Exception as exc:
        return False, str(exc)
    finally:
        if restore_address is not None:
            try:
                await app.state.remna.update_node_address(node_uuid, restore_address)
            except Exception:
                logger.exception("Failed to restore address for node %s", node_uuid)


async def regru_request(namespace: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    with closing(connect_db()) as db:
        username = setting_value(db, "regru_username", "")
        password = setting_value(db, "regru_password", "")
    if not username or not password:
        return {}
    data = {"username": username, "password": password, "input_format": "json", "output_format": "json", "io_encoding": "utf8", "input_data": json.dumps(params, ensure_ascii=False)}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"https://api.reg.ru/api/regru2/{namespace}/{method}", data=data)
        response.raise_for_status()
        payload = response.json()
    if payload.get("result") != "success":
        raise RuntimeError(f"REG.RU: {payload.get('error_code')} {payload.get('error_text', '')}")
    return payload.get("answer") or {}


async def load_managed_domains(app: FastAPI) -> dict[str, Any]:
    hosts = await app.state.remna.hosts()
    host_names = sorted({str(item.get("address", "")).lower().rstrip(".") for item in hosts if item.get("address") and not re.fullmatch(r"[0-9a-fA-F:.]+", str(item["address"]))})
    result: dict[str, Any] = {"hosts": hosts, "domains": []}
    try:
        answer = await regru_request("service", "get_list", {"servtype": "domain"})
        for item in answer.get("services", []):
            zone = str(item.get("dname", "")).lower().rstrip(".")
            matches = [name for name in host_names if name == zone or name.endswith(f".{zone}")]
            if matches:
                result["domains"].append({"provider": "REG.RU", "zone": zone, "hosts": matches})
    except Exception:
        logger.exception("Failed to load REG.RU domains")
    with closing(connect_db()) as db:
        token = setting_value(db, "cloudflare_api_token", "")
    if token:
        try:
            async with httpx.AsyncClient(base_url="https://api.cloudflare.com/client/v4", headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
                response = await client.get("/zones", params={"per_page": 50})
                response.raise_for_status()
                for zone in response.json().get("result", []):
                    name = str(zone.get("name", "")).lower().rstrip(".")
                    matches = [host for host in host_names if host == name or host.endswith(f".{name}")]
                    if matches:
                        result["domains"].append({"provider": "Cloudflare", "zone": name, "zone_id": zone.get("id"), "hosts": matches})
        except Exception:
            logger.exception("Failed to load Cloudflare domains")
    matched = {host for item in result["domains"] for host in item["hosts"]}
    result["domains"].extend({"provider": "Не найден", "zone": host, "hosts": [host]} for host in host_names if host not in matched)
    with closing(connect_db()) as db:
        node_info = {row["uuid"]: dict(row) for row in db.execute("SELECT uuid,name,address,country_code FROM nodes")}
    for domain in result["domains"]:
        uuids = {uuid for host in hosts if str(host.get("address", "")).lower().rstrip(".") in domain["hosts"] for uuid in (host.get("nodes") or [])}
        domain["nodes"] = [{"uuid": uuid, **node_info.get(uuid, {"name": uuid, "address": "", "country_code": ""})} for uuid in uuids]
    return result


def invalidate_managed_domains_cache() -> None:
    global managed_domains_cache, managed_domains_cache_at
    managed_domains_cache = None
    managed_domains_cache_at = 0.0


async def managed_domains(
    app: FastAPI,
    force_refresh: bool = False,
) -> dict[str, Any]:
    global managed_domains_cache, managed_domains_cache_at
    now = time.monotonic()
    if (
        not force_refresh
        and managed_domains_cache is not None
        and now - managed_domains_cache_at < MANAGED_DOMAINS_CACHE_SECONDS
    ):
        return managed_domains_cache
    async with managed_domains_cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and managed_domains_cache is not None
            and now - managed_domains_cache_at < MANAGED_DOMAINS_CACHE_SECONDS
        ):
            return managed_domains_cache
        managed_domains_cache = await load_managed_domains(app)
        managed_domains_cache_at = time.monotonic()
        return managed_domains_cache


def dpi_result_unavailable(result: dict[str, Any]) -> bool:
    samples = [
        item
        for item in result.get("results", [])
        if item.get("is_direct") is not True
        and (
            item.get("mode") == "server"
            or "accessible" in item
            or "connected" in item
        )
    ]
    if not samples:
        return True
    return not any(
        item.get("accessible") is True or item.get("connected") is True
        for item in samples
    )


async def replace_managed_dns(app: FastAPI, node_uuid: str, old_ip: str, new_ip: str, force: bool = False) -> list[dict[str, Any]]:
    with closing(connect_db()) as db:
        enabled = db.execute("SELECT auto_dns_replace FROM node_settings WHERE node_uuid=?", (node_uuid,)).fetchone()
        cf_token = setting_value(db, "cloudflare_api_token", "")
        cf_ttl = int(setting_value(db, "cloudflare_ttl", "600"))
        regru_ttl = int(setting_value(db, "regru_ttl", "600"))
        node = db.execute("SELECT * FROM nodes WHERE uuid=?", (node_uuid,)).fetchone()
    if not force and (not enabled or not enabled["auto_dns_replace"]):
        return []
    changes: list[dict[str, Any]] = []
    hosts = [item for item in await app.state.remna.hosts() if node_uuid in (item.get("nodes") or [])]
    names = {str(item.get("address", "")).lower().rstrip(".") for item in hosts}
    domains = await managed_domains(app, force_refresh=True)
    if cf_token:
        async with httpx.AsyncClient(base_url="https://api.cloudflare.com/client/v4", headers={"Authorization": f"Bearer {cf_token}"}, timeout=30) as client:
            for zone in (item for item in domains["domains"] if item["provider"] == "Cloudflare"):
                records = (await client.get(f"/zones/{zone['zone_id']}/dns_records", params={"per_page": 100})).json().get("result", [])
                for record in records:
                    if record.get("name", "").lower() in names and record.get("content") == old_ip and record.get("type") in {"A", "AAAA"}:
                        response = await client.patch(f"/zones/{zone['zone_id']}/dns_records/{record['id']}", json={"content": new_ip, "ttl": cf_ttl})
                        response.raise_for_status()
                        changes.append({"provider": "Cloudflare", "domain": record.get("name"), "old_ip": old_ip, "new_ip": new_ip, "ttl": cf_ttl})
    regru_zones = [item["zone"] for item in domains["domains"] if item["provider"] == "REG.RU"]
    if regru_zones:
        answer = await regru_request("zone", "get_resource_records", {"domains": [{"dname": zone} for zone in regru_zones]})
        for zone in answer.get("domains", []):
            dname = zone.get("dname", "")
            for record in zone.get("rrs", []):
                fqdn = dname if record.get("subname") in {"", "@"} else f"{record.get('subname')}.{dname}"
                if fqdn.lower() in names and record.get("content") == old_ip and str(record.get("rectype", "")).upper() in {"A", "AAAA"}:
                    method = "add_alias" if ipaddress.ip_address(new_ip).version == 4 else "add_aaaa"
                    await regru_request("zone", method, {"domains": [{"dname": dname}], "subdomain": record.get("subname") or "@", "ipaddr": new_ip, "ttl": regru_ttl})
                    await regru_request("zone", "remove_record", {"domains": [{"dname": dname}], "subdomain": record.get("subname") or "@", "record_type": record.get("rectype"), "content": old_ip})
                    changes.append({"provider": "REG.RU", "domain": fqdn, "old_ip": old_ip, "new_ip": new_ip, "ttl": regru_ttl})
    for change in changes:
        with closing(connect_db()) as db:
            db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (node_uuid, "domain.ip_changed", json.dumps(change), utc_now()))
            db.commit()
        if node:
            await send_webhooks("domain.ip_changed", dict(node), change)
            await telegram_notify("events", f"<b>DNS-запись изменена</b>\nДомен: <code>{html.escape(str(change['domain']))}</code>\n{html.escape(old_ip)} → {html.escape(new_ip)}", "domain.ip_changed")
    if changes:
        invalidate_managed_domains_cache()
    return changes


async def rotate_node_ip(app: FastAPI, node_uuid: str) -> None:
    if node_uuid in ip_rotation_tasks:
        return
    ip_rotation_tasks.add(node_uuid)
    try:
        with closing(connect_db()) as db:
            config = db.execute("SELECT auto_ip_replace,max_ip_replacements,ip_replacements FROM node_settings WHERE node_uuid=?", (node_uuid,)).fetchone()
            node = db.execute("SELECT * FROM nodes WHERE uuid=?", (node_uuid,)).fetchone()
            candidates = db.execute("SELECT * FROM node_ip_pool WHERE node_uuid=? AND status='valid' AND address!=? ORDER BY priority,id", (node_uuid, node["address"] if node else "")).fetchall()
        if not config or not config["auto_ip_replace"] or not node or int(config["ip_replacements"]) >= int(config["max_ip_replacements"]):
            return
        original = node["address"]
        for candidate in candidates:
            ok, error = await test_node_address(app, node_uuid, candidate["address"], None)
            if ok:
                with closing(connect_db()) as db:
                    db.execute("UPDATE nodes SET address=? WHERE uuid=?", (candidate["address"], node_uuid))
                    db.execute("UPDATE node_settings SET ip_replacements=ip_replacements+1 WHERE node_uuid=?", (node_uuid,))
                    payload = {"old_address": original, "new_address": candidate["address"], "automatic": True}
                    db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (node_uuid, "node.ip_changed", json.dumps(payload), utc_now()))
                    db.commit()
                await send_webhooks("node.ip_changed", dict(node), payload)
                await telegram_notify("events", event_message("node.ip_changed", dict(node), payload), "node.ip_changed")
                try:
                    await replace_managed_dns(app, node_uuid, original, candidate["address"])
                except Exception:
                    logger.exception("Automatic DNS replacement failed for node %s", node_uuid)
                return
            with closing(connect_db()) as db:
                db.execute("UPDATE node_ip_pool SET status='invalid',error=?,last_tested=? WHERE id=?", (error, utc_now(), candidate["id"]))
                db.commit()
        await app.state.remna.update_node_address(node_uuid, original)
    finally:
        ip_rotation_tasks.discard(node_uuid)


def effective_history_days(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM app_settings WHERE key='history_days'").fetchone()
    return max(1, min(365, int(row["value"]))) if row else settings.history_days


def setting_value(db: sqlite3.Connection, key: str, default: str) -> str:
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def dpi_is_configured() -> bool:
    try:
        with closing(connect_db()) as db:
            return bool(setting_value(db, "dpi_api_key", settings.dpi_api_key or ""))
    except sqlite3.Error:
        return bool(settings.dpi_api_key)


async def run_scheduled_dpi() -> None:
    if not dpi_is_configured():
        return
    with closing(connect_db()) as db:
        enabled = setting_value(db, "dpi_schedule_enabled", "false") == "true"
        mode = setting_value(db, "dpi_schedule_mode", "interval")
        interval = max(15, int(setting_value(db, "dpi_interval_minutes", "360")))
        schedule_time = setting_value(db, "dpi_schedule_time", "03:00")
        weekly_schedule = json.loads(setting_value(db, "dpi_weekly_schedule", "[]"))
        last_run = setting_value(db, "dpi_last_run", "")
        due = not last_run or datetime.fromisoformat(last_run) <= datetime.now(timezone.utc) - timedelta(minutes=interval)
        if mode == "daily":
            zone_name = setting_value(db, "timezone", settings.panel_timezone)
            local_now = datetime.now(ZoneInfo(zone_name))
            today = [item for item in weekly_schedule if int(item.get("day", -1)) == local_now.weekday() and item.get("enabled", True)]
            if not today:
                return
            local_last = datetime.fromisoformat(last_run).astimezone(ZoneInfo(zone_name)) if last_run else None
            due = False
            for item in today:
                hour, minute = (int(part) for part in str(item.get("time", schedule_time)).split(":", 1))
                scheduled_at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if scheduled_at <= local_now and (not local_last or local_last < scheduled_at):
                    due = True
                    break
        if not enabled or not due:
            return
        selected = json.loads(setting_value(db, "dpi_schedule_nodes", '[]'))
        pop_ids = json.loads(setting_value(db, "dpi_schedule_pop_ids", "[]"))
        query = "SELECT uuid,address FROM nodes WHERE address!=''" + (f" AND uuid IN ({','.join('?' for _ in selected)})" if selected else "")
        nodes = db.execute(query, selected).fetchall()
        db.execute("INSERT INTO app_settings(key,value) VALUES('dpi_last_run',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utc_now(),))
        db.commit()
    for node in nodes:
        asyncio.create_task(queue_dpi(node["uuid"], node["address"], "russia", pop_ids or None))


async def monitor_loop(app: FastAPI) -> None:
    last_polled: dict[str, float] = {}
    while True:
        started = time.monotonic()
        loop_interval = settings.poll_interval_seconds
        try:
            with closing(connect_db()) as db:
                global_interval = int(setting_value(db, "poll_interval_seconds", str(settings.poll_interval_seconds)))
                configured_intervals = {row["node_uuid"]: int(row["poll_interval_seconds"]) for row in db.execute("SELECT node_uuid,poll_interval_seconds FROM node_settings WHERE poll_interval_seconds IS NOT NULL")}
            loop_interval = min([global_interval, *configured_intervals.values()])
            raw_nodes = await app.state.remna.nodes()
            try:
                metrics_by_node = await app.state.remna.metrics()
            except httpx.HTTPError:
                metrics_by_node = {}
                logger.warning("Remnawave metrics unavailable; add system:nodes-metrics permission")
            for raw in raw_nodes:
                node = normalize_node(raw)
                node_interval = configured_intervals.get(node["uuid"], global_interval)
                if started - last_polled.get(node["uuid"], 0) < node_interval:
                    continue
                node["metric"] = metrics_by_node.get(node["uuid"], {})
                if node["metric"].get("usersOnline") is not None:
                    node["online"] = int(node["metric"]["usersOnline"])
                await process_node(node)
                if not node["connected"]:
                    asyncio.create_task(rotate_node_ip(app, node["uuid"]))
                last_polled[node["uuid"]] = started
            await run_scheduled_dpi()
            await check_dpi_balance()
            with closing(connect_db()) as db:
                global_days = effective_history_days(db)
                nodes = db.execute("SELECT n.uuid,COALESCE(s.retention_days,?) days FROM nodes n LEFT JOIN node_settings s ON s.node_uuid=n.uuid", (global_days,)).fetchall()
                for node in nodes:
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(node["days"]))).isoformat()
                    db.execute("DELETE FROM samples WHERE node_uuid=? AND created_at<?", (node["uuid"], cutoff))
                db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Monitoring iteration failed")
        await asyncio.sleep(max(1, loop_interval - (time.monotonic() - started)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    root_logger = logging.getLogger()
    if not any(isinstance(handler, DatabaseLogHandler) for handler in root_logger.handlers):
        root_logger.addHandler(DatabaseLogHandler())
    root_logger.setLevel(logging.INFO)
    app.state.remna = create_remnawave_client()
    for icon_url in INTEGRATION_ICON_URLS.values():
        icon_cache.schedule(icon_url, trusted=True)
    task = asyncio.create_task(monitor_loop(app))
    with closing(connect_db()) as db:
        pending_dpi = db.execute("SELECT id,provider_id FROM dpi_checks WHERE status IN ('pending','active') AND provider_id IS NOT NULL").fetchall()
    dpi_tasks = [asyncio.create_task(follow_dpi_check(int(row["id"]), int(row["provider_id"]))) for row in pending_dpi]
    yield
    task.cancel()
    for dpi_task in dpi_tasks:
        dpi_task.cancel()
    await asyncio.gather(task, *dpi_tasks, return_exceptions=True)
    await app.state.remna.close()


app = FastAPI(title="RemnaDownDetector", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret, https_only=settings.secure_cookies, same_site="strict", max_age=28800)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/integration-icon/{name}")
async def integration_icon(name: str, _: str = Depends(require_user)):
    url = INTEGRATION_ICON_URLS.get(name)
    if not url:
        raise HTTPException(404)
    cached = await icon_cache.get(url, trusted=True)
    if cached:
        content, content_type = cached
        return Response(
            content,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=604800, immutable"},
        )
    initials = html.escape(name[:2].upper())
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        '<rect width="64" height="64" rx="14" fill="#21262d"/>'
        '<text x="32" y="40" text-anchor="middle" font-family="sans-serif" '
        'font-size="20" font-weight="700" fill="#22d3ee">'
        f"{initials}</text></svg>"
    )
    return Response(
        svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=21600"},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", 303)
    request.session["csrf"] = secrets.token_urlsafe(32)
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request, username: str = Form(), password: str = Form(), csrf_token: str = Form()):
    verify_csrf(request, csrf_token)
    ip = client_ip(request)
    attempts = login_attempts[ip]
    now = time.monotonic()
    while attempts and attempts[0] < now - 900:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many login attempts; retry in 15 minutes")
    valid_user = hmac.compare_digest(username.encode(), settings.admin_username.encode())
    valid_password = hmac.compare_digest(password.encode(), settings.admin_password.encode())
    if not (valid_user and valid_password):
        attempts.append(now)
        await telegram_notify("auth", f"<b>⚠️ Неудачный вход</b>\nIP: <code>{html.escape(ip)}</code>\nЛогин: <code>{html.escape(username[:80])}</code>", "auth.failed")
        await asyncio.sleep(min(2.0, 0.3 * len(attempts)))
        return render(request, "login.html", error="Неверный логин или пароль")
    attempts.clear()
    request.session.clear()
    request.session.update({"user": settings.admin_username, "csrf": secrets.token_urlsafe(32)})
    await telegram_notify("auth", f"<b>✅ Вход в панель</b>\nIP: <code>{html.escape(ip)}</code>\nПользователь: <code>{html.escape(settings.admin_username)}</code>", "auth.login")
    return RedirectResponse("/", 303)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(), _: str = Depends(require_user)):
    verify_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return render(request, "dashboard.html", default_threshold=settings.default_drop_percent, dpi_enabled=dpi_is_configured())


@app.get("/api/dashboard")
async def dashboard_data(_: str = Depends(require_user)):
    with closing(connect_db()) as db:
        global_days = effective_history_days(db)
        global_poll = int(setting_value(db, "poll_interval_seconds", str(settings.poll_interval_seconds)))
        nodes = [dict(row) for row in db.execute("SELECT n.*,s.is_down,s.is_unavailable,s.changed_at,COALESCE(c.drop_percent,?) drop_percent,COALESCE(c.webhooks_enabled,1) webhooks_enabled,COALESCE(c.auto_dpi,0) auto_dpi,COALESCE(c.auto_dns_replace,0) auto_dns_replace,COALESCE(c.retention_days,?) retention_days,c.poll_interval_seconds,COALESCE(c.dpi_locations,'[\"russia\"]') dpi_locations,COALESCE(c.dpi_pop_ids,'[]') dpi_pop_ids,COALESCE(c.auto_ip_replace,0) auto_ip_replace,COALESCE(c.max_ip_replacements,1) max_ip_replacements,COALESCE(c.ip_replacements,0) ip_replacements,COALESCE(c.vless_mode,'manual') vless_mode,COALESCE(c.vless_username,'') vless_username,CASE WHEN COALESCE(c.vless_key,'')='' THEN 0 ELSE 1 END vless_key_configured,COALESCE(c.action_chain,'[]') action_chain FROM nodes n LEFT JOIN node_state s ON s.node_uuid=n.uuid LEFT JOIN node_settings c ON c.node_uuid=n.uuid ORDER BY n.view_position,n.name", (settings.default_drop_percent, global_days))]
        for node in nodes:
            thresholds = db.execute("SELECT cpu_threshold,ram_threshold,rx_threshold_mbps,tx_threshold_mbps,dpi_squad_uuids FROM node_settings WHERE node_uuid=?", (node["uuid"],)).fetchone()
            if thresholds:
                node.update(dict(thresholds))
            samples = db.execute("SELECT online,connected,cpu_percent,ram_percent,load_1,load_5,load_15,rx_bps,tx_bps,rx_total,tx_total,created_at FROM samples WHERE node_uuid=? ORDER BY id DESC LIMIT 288", (node["uuid"],)).fetchall()
            node["samples"] = [dict(row) for row in reversed(samples)]
            node["latest_online"] = node["samples"][-1]["online"] if node["samples"] else 0
            node["webhook_ids"] = [row["webhook_id"] for row in db.execute("SELECT webhook_id FROM node_webhooks WHERE node_uuid=?", (node["uuid"],))]
            node["dpi_locations"] = json.loads(node["dpi_locations"])
            node["dpi_pop_ids"] = json.loads(node["dpi_pop_ids"])
            node["action_chain"] = json.loads(node["action_chain"])
            node["dpi_squad_uuids"] = json.loads(node.get("dpi_squad_uuids") or "[]")
            node["ip_pool"] = [dict(row) for row in db.execute("SELECT id,address,priority,status,error,last_tested FROM node_ip_pool WHERE node_uuid=? ORDER BY priority,id", (node["uuid"],))]
        events = [dict(row) for row in db.execute("SELECT e.*,n.name,n.country_code FROM events e JOIN nodes n ON n.uuid=e.node_uuid ORDER BY e.id DESC LIMIT 50")]
        hooks = [{**{k: row[k] for k in ("id", "name", "url", "enabled")}, "events": [item["event"] for item in db.execute("SELECT event FROM webhook_events WHERE webhook_id=? ORDER BY event", (row["id"],))]} for row in db.execute("SELECT * FROM webhooks ORDER BY id")]
        panel_settings = {"history_days": global_days, "poll_interval": global_poll, "timezone": setting_value(db, "timezone", settings.panel_timezone), "dpi_schedule_enabled": setting_value(db, "dpi_schedule_enabled", "false") == "true", "dpi_schedule_mode": setting_value(db, "dpi_schedule_mode", "interval"), "dpi_schedule_time": setting_value(db, "dpi_schedule_time", "03:00"), "dpi_interval_minutes": int(setting_value(db, "dpi_interval_minutes", "360")), "dpi_schedule_nodes": json.loads(setting_value(db, "dpi_schedule_nodes", "[]")), "dpi_schedule_locations": ["russia"], "dpi_schedule_pop_ids": json.loads(setting_value(db, "dpi_schedule_pop_ids", "[]")), "dpi_infra_billing_enabled": setting_value(db, "dpi_infra_billing_enabled", "false") == "true", "dpi_balance_threshold": float(setting_value(db, "dpi_balance_threshold", "1")), "dpi_api_configured": bool(setting_value(db, "dpi_api_key", settings.dpi_api_key or "")), "telegram_enabled": setting_value(db, "telegram_enabled", "false") == "true", "telegram_configured": bool(setting_value(db, "telegram_bot_token", "")), "telegram_chat_id": setting_value(db, "telegram_chat_id", ""), "telegram_events_topic": setting_value(db, "telegram_events_topic", ""), "telegram_auth_topic": setting_value(db, "telegram_auth_topic", ""), "telegram_event_types": json.loads(setting_value(db, "telegram_event_types", "[]")), "regru_ttl": int(setting_value(db, "regru_ttl", "600")), "cloudflare_ttl": int(setting_value(db, "cloudflare_ttl", "600")), "dns_configured": bool(setting_value(db, "cloudflare_api_token", "") or (setting_value(db, "regru_username", "") and setting_value(db, "regru_password", "")))}
        panel_settings["dpi_weekly_schedule"] = json.loads(setting_value(db, "dpi_weekly_schedule", "[]"))
        panel_settings["remnawave_api_version"] = setting_value(db, "remnawave_api_version", "auto")
    try:
        panel_settings["remnawave_detected_version"] = await app.state.remna.panel_version()
    except Exception:
        panel_settings["remnawave_detected_version"] = "unknown"
    return {"nodes": nodes, "events": events, "webhooks": hooks, "settings": panel_settings, "integration_alerts": list(permission_alerts)}


@app.get("/api/online/meta")
async def online_meta(_: str = Depends(require_user)):
    client = create_remnawave_client()
    try:
        return await client.squads()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Не удалось загрузить сквады Remnawave") from exc
    finally:
        await client.close()


@app.get("/api/logs")
async def app_logs(level: str = "all", query: str = "", _: str = Depends(require_user)):
    clauses, params = [], []
    if level != "all":
        clauses.append("level=?")
        params.append(level.upper())
    if query.strip():
        clauses.append("(message LIKE ? OR details LIKE ? OR source LIKE ?)")
        value = f"%{query.strip()}%"
        params.extend([value, value, value])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_db()) as db:
        rows = db.execute(f"SELECT id,level,source,message,details,created_at FROM app_logs {where} ORDER BY id DESC LIMIT 500", params).fetchall()
    return [dict(row) for row in rows]


@app.delete("/api/events")
async def clear_events(request: Request, _: str = Depends(require_user)):
    verify_csrf(request, request.headers.get("X-CSRF-Token", ""))
    with closing(connect_db()) as db:
        cursor = db.execute("DELETE FROM events")
        db.commit()
    logger.info("Event history cleared: %s rows", cursor.rowcount)
    return {"ok": True, "deleted": cursor.rowcount}


@app.put("/api/nodes/{node_uuid}/settings")
async def update_node_settings(node_uuid: str, body: NodeSettingsUpdate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    if not 1 <= body.drop_percent <= 100:
        raise HTTPException(422, "drop_percent must be between 1 and 100")
    if not 1 <= body.retention_days <= 365:
        raise HTTPException(422, "retention_days must be between 1 and 365")
    if body.poll_interval_seconds is not None and not 10 <= body.poll_interval_seconds <= 3600:
        raise HTTPException(422, "Интервал ноды должен быть от 10 до 3600 секунд")
    if body.dpi_locations != ["russia"]:
        raise HTTPException(422, "Unsupported DPI location")
    if body.vless_mode not in {"manual", "remnawave", "temporary"}:
        raise HTTPException(422, "Unsupported VLESS source")
    allowed_actions = {"dpi_ip", "dpi_vless", "rotate_ip", "replace_dns"}
    if len(body.action_chain) > 12 or any(item.get("type") not in allowed_actions for item in body.action_chain):
        raise HTTPException(422, "Invalid action chain")
    enabled_actions = {
        item["type"]
        for item in body.action_chain
        if item.get("enabled", True) and item.get("type") in allowed_actions
    }
    if len(body.dpi_squad_uuids) > 100 or any(not re.fullmatch(r"[0-9a-fA-F-]{36}", item) for item in body.dpi_squad_uuids):
        raise HTTPException(422, "Invalid internal squad selection")
    with closing(connect_db()) as db:
        db.execute("INSERT INTO node_settings(node_uuid,drop_percent,webhooks_enabled,auto_dpi,auto_dns_replace,cpu_threshold,ram_threshold,rx_threshold_mbps,tx_threshold_mbps,retention_days,poll_interval_seconds,dpi_locations,dpi_pop_ids,vless_mode,vless_key,vless_username,action_chain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(node_uuid) DO UPDATE SET drop_percent=excluded.drop_percent,webhooks_enabled=excluded.webhooks_enabled,auto_dpi=excluded.auto_dpi,auto_dns_replace=excluded.auto_dns_replace,cpu_threshold=excluded.cpu_threshold,ram_threshold=excluded.ram_threshold,rx_threshold_mbps=excluded.rx_threshold_mbps,tx_threshold_mbps=excluded.tx_threshold_mbps,retention_days=excluded.retention_days,poll_interval_seconds=excluded.poll_interval_seconds,dpi_locations=excluded.dpi_locations,dpi_pop_ids=excluded.dpi_pop_ids,vless_mode=excluded.vless_mode,vless_key=CASE WHEN excluded.vless_key='' THEN node_settings.vless_key ELSE excluded.vless_key END,vless_username=excluded.vless_username,action_chain=excluded.action_chain", (node_uuid, body.drop_percent, int(body.webhooks_enabled), int(body.auto_dpi), int("replace_dns" in enabled_actions), body.cpu_threshold, body.ram_threshold, body.rx_threshold_mbps, body.tx_threshold_mbps, body.retention_days, body.poll_interval_seconds, json.dumps(body.dpi_locations), json.dumps(body.dpi_pop_ids), body.vless_mode, body.vless_key.strip(), body.vless_username.strip(), json.dumps(body.action_chain)))
        db.execute(
            "UPDATE node_settings SET auto_ip_replace=? WHERE node_uuid=?",
            (int("rotate_ip" in enabled_actions), node_uuid),
        )
        db.execute("DELETE FROM node_webhooks WHERE node_uuid=?", (node_uuid,))
        db.execute("UPDATE node_settings SET dpi_squad_uuids=? WHERE node_uuid=?", (json.dumps(sorted(set(body.dpi_squad_uuids))), node_uuid))
        db.executemany("INSERT OR IGNORE INTO node_webhooks(node_uuid,webhook_id) VALUES(?,?)", [(node_uuid, hook_id) for hook_id in body.webhook_ids])
        db.commit()
    return {"ok": True}


@app.post("/api/nodes/{node_uuid}/ips")
async def add_node_ip(node_uuid: str, body: IpPoolCreate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    try:
        ipaddress.ip_address(body.address)
    except ValueError as exc:
        raise HTTPException(422, "Некорректный IP-адрес") from exc
    if not 1 <= body.priority <= 10000:
        raise HTTPException(422, "Приоритет должен быть от 1 до 10000")
    with closing(connect_db()) as db:
        node = db.execute("SELECT address FROM nodes WHERE uuid=?", (node_uuid,)).fetchone()
        if not node:
            raise HTTPException(404, "Node not found")
        cursor = db.execute("INSERT INTO node_ip_pool(node_uuid,address,priority) VALUES(?,?,?) ON CONFLICT(node_uuid,address) DO UPDATE SET priority=excluded.priority RETURNING id", (node_uuid, body.address, body.priority))
        row_id = int(cursor.fetchone()["id"])
        db.commit()
    ok, error = await test_node_address(request.app, node_uuid, body.address, node["address"])
    with closing(connect_db()) as db:
        db.execute("UPDATE node_ip_pool SET status=?,error=?,last_tested=? WHERE id=?", ("valid" if ok else "invalid", error, utc_now(), row_id))
        db.commit()
    return {"id": row_id, "valid": ok, "error": error}


@app.put("/api/nodes/{node_uuid}/ips/{ip_id}")
async def update_node_ip(node_uuid: str, ip_id: int, body: IpPoolUpdate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    if not 1 <= body.priority <= 10000:
        raise HTTPException(422, "Приоритет должен быть от 1 до 10000")
    with closing(connect_db()) as db:
        db.execute("UPDATE node_ip_pool SET priority=? WHERE id=? AND node_uuid=?", (body.priority, ip_id, node_uuid))
        db.commit()
    return {"ok": True}


@app.post("/api/nodes/{node_uuid}/ips/{ip_id}/recheck")
async def recheck_node_ip(node_uuid: str, ip_id: int, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, request.headers.get("X-CSRF-Token", ""))
    with closing(connect_db()) as db:
        node = db.execute("SELECT address FROM nodes WHERE uuid=?", (node_uuid,)).fetchone()
        item = db.execute("SELECT address FROM node_ip_pool WHERE id=? AND node_uuid=?", (ip_id, node_uuid)).fetchone()
    if not node or not item:
        raise HTTPException(404, "IP address not found")
    ok, error = await test_node_address(request.app, node_uuid, item["address"], node["address"])
    with closing(connect_db()) as db:
        db.execute("UPDATE node_ip_pool SET status=?,error=?,last_tested=? WHERE id=? AND node_uuid=?", ("valid" if ok else "invalid", error, utc_now(), ip_id, node_uuid))
        db.commit()
    return {"valid": ok, "error": error}


@app.post("/api/nodes/{node_uuid}/ips/{ip_id}/activate")
async def activate_node_ip(node_uuid: str, ip_id: int, body: IpActivate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    with closing(connect_db()) as db:
        node = db.execute("SELECT * FROM nodes WHERE uuid=?", (node_uuid,)).fetchone()
        item = db.execute("SELECT * FROM node_ip_pool WHERE id=? AND node_uuid=?", (ip_id, node_uuid)).fetchone()
        dns_configured = bool(setting_value(db, "cloudflare_api_token", "") or (setting_value(db, "regru_username", "") and setting_value(db, "regru_password", "")))
    if not node or not item:
        raise HTTPException(404, "IP address not found")
    if item["status"] != "valid":
        raise HTTPException(409, "Сначала перепроверьте IP-адрес")
    old_ip, new_ip = node["address"], item["address"]
    if old_ip == new_ip:
        return {"ok": True, "dns_configured": dns_configured, "domains_changed": 0}
    await request.app.state.remna.update_node_address(node_uuid, new_ip)
    payload = {"old_address": old_ip, "new_address": new_ip, "automatic": False}
    with closing(connect_db()) as db:
        db.execute("UPDATE nodes SET address=? WHERE uuid=?", (new_ip, node_uuid))
        last_priority = db.execute("SELECT COALESCE(MAX(priority),0)+1 value FROM node_ip_pool WHERE node_uuid=?", (node_uuid,)).fetchone()["value"]
        db.execute("DELETE FROM node_ip_pool WHERE id=? AND node_uuid=?", (ip_id, node_uuid))
        db.execute("INSERT INTO node_ip_pool(node_uuid,address,priority,status,error,last_tested) VALUES(?,?,?,'valid','',?) ON CONFLICT(node_uuid,address) DO UPDATE SET priority=excluded.priority,status='valid',error='',last_tested=excluded.last_tested", (node_uuid, old_ip, last_priority, utc_now()))
        db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (node_uuid, "node.ip_changed", json.dumps(payload), utc_now()))
        db.commit()
    await send_webhooks("node.ip_changed", dict(node), payload)
    await telegram_notify("events", event_message("node.ip_changed", dict(node), payload), "node.ip_changed")
    changes = await replace_managed_dns(request.app, node_uuid, old_ip, new_ip, force=body.update_domains) if body.update_domains else []
    return {"ok": True, "dns_configured": dns_configured, "domains_changed": len(changes)}


@app.put("/api/nodes/{node_uuid}/ips/actions/reorder")
async def reorder_node_ips(node_uuid: str, body: IpPoolReorder, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    if len(body.ids) != len(set(body.ids)) or len(body.ids) > 100:
        raise HTTPException(422, "Invalid IP order")
    with closing(connect_db()) as db:
        existing = {row["id"] for row in db.execute("SELECT id FROM node_ip_pool WHERE node_uuid=?", (node_uuid,))}
        if set(body.ids) != existing:
            raise HTTPException(422, "IP order does not match the node pool")
        for index, ip_id in enumerate(body.ids, start=1):
            db.execute("UPDATE node_ip_pool SET priority=? WHERE id=? AND node_uuid=?", (index, ip_id, node_uuid))
        db.commit()
    return {"ok": True}


@app.delete("/api/nodes/{node_uuid}/ips/{ip_id}")
async def delete_node_ip(node_uuid: str, ip_id: int, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, request.headers.get("X-CSRF-Token", ""))
    with closing(connect_db()) as db:
        db.execute("DELETE FROM node_ip_pool WHERE id=? AND node_uuid=?", (ip_id, node_uuid))
        db.commit()
    return {"ok": True}


@app.put("/api/nodes/{node_uuid}/ip-rotation")
async def update_ip_rotation(node_uuid: str, body: IpRotationUpdate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    if not 1 <= body.max_replacements <= 100:
        raise HTTPException(422, "Количество замен должно быть от 1 до 100")
    with closing(connect_db()) as db:
        db.execute("INSERT INTO node_settings(node_uuid,auto_ip_replace,max_ip_replacements) VALUES(?,?,?) ON CONFLICT(node_uuid) DO UPDATE SET auto_ip_replace=excluded.auto_ip_replace,max_ip_replacements=excluded.max_ip_replacements", (node_uuid, int(body.enabled), body.max_replacements))
        db.commit()
    return {"ok": True}


@app.get("/api/nodes/{node_uuid}/history")
async def node_history(node_uuid: str, days: int = 1, _: str = Depends(require_user)):
    days = max(1, min(365, days))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with closing(connect_db()) as db:
        rows = [dict(row) for row in db.execute("SELECT online,connected,cpu_percent,ram_percent,load_1,load_5,load_15,rx_bps,tx_bps,rx_total,tx_total,created_at FROM samples WHERE node_uuid=? AND created_at>=? ORDER BY created_at", (node_uuid, cutoff))]
    if len(rows) > 1600:
        step = max(1, len(rows) // 1500)
        rows = rows[::step]
    return {"samples": rows, "days": days}


@app.get("/api/nodes/{node_uuid}/provider-icon")
async def provider_icon(node_uuid: str, _: str = Depends(require_user)):
    with closing(connect_db()) as db:
        row = db.execute("SELECT provider_icon,provider_name FROM nodes WHERE uuid=?", (node_uuid,)).fetchone()
    if not row:
        raise HTTPException(404, "Node not found")
    url = row["provider_icon"] if row else ""
    cached = await icon_cache.get(
        str(url),
        is_allowed=public_webhook_url,
    )
    if cached:
        content, content_type = cached
        return Response(
            content,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=604800, immutable"},
        )
    initials = "".join(part[:1] for part in str(row["provider_name"] or "?").split())[:2].upper()
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><defs><linearGradient id="g"><stop stop-color="#5765f2"/><stop offset="1" stop-color="#32d6ba"/></linearGradient></defs><rect width="64" height="64" rx="14" fill="url(#g)"/><text x="32" y="40" text-anchor="middle" font-family="sans-serif" font-size="22" font-weight="700" fill="#fff">{html.escape(initials)}</text></svg>'
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"})


@app.put("/api/settings")
async def update_app_settings(body: AppSettingsUpdate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    if not 1 <= body.history_days <= 365:
        raise HTTPException(422, "history_days must be between 1 and 365")
    if not 10 <= body.poll_interval_seconds <= 3600:
        raise HTTPException(422, "Общий интервал должен быть от 10 до 3600 секунд")
    if not 15 <= body.dpi_interval_minutes <= 43200:
        raise HTTPException(422, "DPI interval must be between 15 and 43200 minutes")
    if body.dpi_schedule_mode not in {"interval", "daily"}:
        raise HTTPException(422, "Unknown schedule mode")
    if body.remnawave_api_version not in {"auto", "2.7.4", "latest"}:
        raise HTTPException(422, "Unknown Remnawave API version")
    if len(body.dpi_weekly_schedule) > 70:
        raise HTTPException(422, "Too many schedule entries")
    for item in body.dpi_weekly_schedule:
        if not 0 <= int(item.get("day", -1)) <= 6:
            raise HTTPException(422, "Invalid schedule day")
        try:
            datetime.strptime(str(item.get("time", "")), "%H:%M")
        except ValueError as exc:
            raise HTTPException(422, "Schedule time must be HH:MM") from exc
    try:
        datetime.strptime(body.dpi_schedule_time, "%H:%M")
    except ValueError as exc:
        raise HTTPException(422, "Schedule time must be HH:MM") from exc
    if body.dpi_schedule_locations != ["russia"]:
        raise HTTPException(422, "Unsupported DPI location")
    if len(body.dpi_schedule_pop_ids) > 100 or any(pop_id < 1 for pop_id in body.dpi_schedule_pop_ids):
        raise HTTPException(422, "Unsupported DPI PoP selection")
    if not 0 <= body.dpi_balance_threshold <= 100000:
        raise HTTPException(422, "Invalid DPI balance threshold")
    if not 60 <= body.regru_ttl <= 86400 or not 60 <= body.cloudflare_ttl <= 86400:
        raise HTTPException(422, "DNS TTL must be between 60 and 86400 seconds")
    try:
        ZoneInfo(body.timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(422, "Unknown timezone") from exc
    if body.telegram_bot_token and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,}", body.telegram_bot_token):
        raise HTTPException(422, "Некорректный токен Telegram-бота")
    if body.telegram_chat_id and not re.fullmatch(r"-?\d+", body.telegram_chat_id):
        raise HTTPException(422, "Некорректный Telegram chat ID")
    for topic in (body.telegram_events_topic, body.telegram_auth_topic):
        if topic and (not topic.isdigit() or int(topic) < 1):
            raise HTTPException(422, "ID топика должен быть положительным числом")
    with closing(connect_db()) as db:
        values = {"history_days": str(body.history_days), "poll_interval_seconds": str(body.poll_interval_seconds), "timezone": body.timezone, "dpi_schedule_enabled": str(body.dpi_schedule_enabled).lower(), "dpi_schedule_mode": body.dpi_schedule_mode, "dpi_schedule_time": body.dpi_schedule_time, "dpi_interval_minutes": str(body.dpi_interval_minutes), "dpi_schedule_nodes": json.dumps(body.dpi_schedule_nodes), "dpi_schedule_locations": '["russia"]', "dpi_schedule_pop_ids": json.dumps(body.dpi_schedule_pop_ids), "dpi_infra_billing_enabled": str(body.dpi_infra_billing_enabled).lower(), "dpi_balance_threshold": str(body.dpi_balance_threshold), "regru_ttl": str(body.regru_ttl), "cloudflare_ttl": str(body.cloudflare_ttl)}
        values["dpi_weekly_schedule"] = json.dumps(body.dpi_weekly_schedule)
        values["remnawave_api_version"] = body.remnawave_api_version
        if body.dpi_api_key:
            values["dpi_api_key"] = body.dpi_api_key
        values.update({"telegram_enabled": str(body.telegram_enabled).lower(), "telegram_chat_id": body.telegram_chat_id, "telegram_events_topic": body.telegram_events_topic, "telegram_auth_topic": body.telegram_auth_topic, "telegram_event_types": json.dumps(sorted(set(body.telegram_event_types)))})
        if body.regru_username:
            values["regru_username"] = body.regru_username
        if body.regru_password:
            values["regru_password"] = body.regru_password
        if body.cloudflare_api_token:
            values["cloudflare_api_token"] = body.cloudflare_api_token
        if body.telegram_bot_token:
            values["telegram_bot_token"] = body.telegram_bot_token
        db.executemany("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())
        db.commit()
    invalidate_managed_domains_cache()
    return {"ok": True}


@app.get("/api/domains")
async def domains_data(
    request: Request,
    refresh: bool = False,
    _: str = Depends(require_user),
):
    return await managed_domains(request.app, force_refresh=refresh)


@app.post("/api/webhooks")
async def create_webhook(body: WebhookCreate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    if not body.name.strip() or not public_webhook_url(body.url):
        raise HTTPException(422, "Webhook must have a name and a public HTTPS URL")
    with closing(connect_db()) as db:
        cursor = db.execute("INSERT INTO webhooks(name,url,secret,enabled) VALUES(?,?,?,1)", (body.name.strip()[:80], body.url, body.secret))
        db.executemany("INSERT INTO webhook_events(webhook_id,event) VALUES(?,?)", [(cursor.lastrowid, event[:80]) for event in set(body.events)])
        db.commit()
    return {"ok": True}


@app.put("/api/webhooks/{webhook_id}/events")
async def update_webhook_events(webhook_id: int, body: WebhookEventsUpdate, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, body.csrf_token)
    with closing(connect_db()) as db:
        db.execute("UPDATE webhooks SET enabled=? WHERE id=?", (int(body.enabled), webhook_id))
        db.execute("DELETE FROM webhook_events WHERE webhook_id=?", (webhook_id,))
        db.executemany("INSERT INTO webhook_events(webhook_id,event) VALUES(?,?)", [(webhook_id, event[:80]) for event in set(body.events)])
        db.commit()
    return {"ok": True}


@app.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int, request: Request, _: str = Depends(require_user)):
    verify_csrf(request, request.headers.get("X-CSRF-Token", ""))
    with closing(connect_db()) as db:
        webhook = db.execute(
            "SELECT id FROM webhooks WHERE id=?",
            (webhook_id,),
        ).fetchone()
        if webhook is None:
            raise HTTPException(404, "Webhook not found")
        db.execute("DELETE FROM node_webhooks WHERE webhook_id=?", (webhook_id,))
        db.execute("DELETE FROM webhook_events WHERE webhook_id=?", (webhook_id,))
        db.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
        db.commit()
    return {"ok": True}


@app.post("/api/webhooks/{webhook_id}/delete")
async def delete_webhook_post(
    webhook_id: int,
    request: Request,
    _: str = Depends(require_user),
):
    payload = await request.json()
    verify_csrf(request, str(payload.get("csrf_token", "")))
    with closing(connect_db()) as db:
        webhook = db.execute(
            "SELECT id FROM webhooks WHERE id=?",
            (webhook_id,),
        ).fetchone()
        if webhook is None:
            raise HTTPException(404, "Webhook not found")
        db.execute("DELETE FROM node_webhooks WHERE webhook_id=?", (webhook_id,))
        db.execute("DELETE FROM webhook_events WHERE webhook_id=?", (webhook_id,))
        db.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
        db.commit()
    return {"ok": True}


@app.post("/api/nodes/{node_uuid}/dpi")
async def dpi_check(
    node_uuid: str,
    body: DpiRequest,
    request: Request,
    _: str = Depends(require_user),
):
    verify_csrf(request, body.csrf_token)
    if body.locations != ["russia"]:
        raise HTTPException(422, "Unsupported location")
    if len(body.pop_ids) > 100 or any(pop_id < 1 for pop_id in body.pop_ids):
        raise HTTPException(422, "Unsupported PoP selection")
    if body.kind not in {"ip", "vless"}:
        raise HTTPException(422, "Unsupported DPI check kind")
    with closing(connect_db()) as db:
        node = db.execute("SELECT * FROM nodes WHERE uuid=?", (node_uuid,)).fetchone()
    if not node or not node["address"]:
        raise HTTPException(404, "Node address not found")
    try:
        if body.kind == "vless":
            target, source, temp_user, context = await vless_target(node_uuid)
        else:
            target, source, temp_user = node["address"], "manual", ""
            context = {}
    except Exception as exc:
        detail = integration_error(exc)
        logger.exception(
            "Unable to prepare %s DPI check for node %s: %s",
            body.kind,
            node_uuid,
            detail,
        )
        status_code = 403 if "HTTP 401" in detail or "HTTP 403" in detail else 502
        raise HTTPException(status_code, detail) from exc
    local_ids = [
        await queue_dpi(
            node_uuid,
            target,
            location,
            body.pop_ids if location == "russia" else None,
            kind=body.kind,
            source=source,
            temp_user_uuid=temp_user,
            context=context,
        )
        for location in body.locations
    ]
    with closing(connect_db()) as db:
        placeholders = ",".join("?" for _ in local_ids)
        failed = db.execute(
            f"SELECT result FROM dpi_checks WHERE id IN ({placeholders}) "
            "AND status='failed' LIMIT 1",
            local_ids,
        ).fetchone()
    if failed:
        error = json.loads(failed["result"]).get("error", "DPI check failed")
        status_code = 403 if "HTTP 403" in error or "permission" in error.lower() else 502
        raise HTTPException(status_code, error)
    return {"queued": True, "local_ids": local_ids}


@app.post("/api/dpi/callback/{token}")
async def dpi_callback(token: str, request: Request):
    if not hmac.compare_digest(token, dpi_callback_token()):
        raise HTTPException(404, "Not found")
    result = await request.json()
    provider_id = result.get("id") or result.get("check_id")
    if not provider_id:
        raise HTTPException(422, "Missing check id")
    with closing(connect_db()) as db:
        row = db.execute(
            "SELECT id,node_uuid,target,automatic,status,kind,source,"
            "temp_user_uuid,context FROM dpi_checks WHERE provider_id=? "
            "ORDER BY id DESC LIMIT 1",
            (provider_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Unknown check")
        if row["status"] in {"completed", "failed", "cancelled"}:
            return {"ok": True, "duplicate": True}
        db.execute("UPDATE dpi_checks SET status=?,result=?,updated_at=? WHERE id=?", (str(result.get("status", "completed")), json.dumps(result), utc_now(), row["id"]))
        shown_target = "[VLESS hidden]" if row["kind"] == "vless" else row["target"]
        completed = {
            **json.loads(row["context"] or "{}"),
            "target": shown_target,
            "kind": row["kind"],
            "source": row["source"],
            "status": result.get("status"),
            "results": result.get("results", []),
        }
        db.execute("INSERT INTO events(node_uuid,event,payload,created_at) VALUES(?,?,?,?)", (row["node_uuid"], "dpi.check_completed", json.dumps(completed), utc_now()))
        db.commit()
    await record_dpi_billing(int(row["id"]), result)
    with closing(connect_db()) as db:
        event_node = db.execute("SELECT * FROM nodes WHERE uuid=?", (row["node_uuid"],)).fetchone()
    if event_node:
        await send_webhooks("dpi.check_completed", dict(event_node), completed)
    if row["temp_user_uuid"]:
        try:
            await request.app.state.remna.delete_user(row["temp_user_uuid"])
        except Exception:
            logger.exception("Failed to delete temporary DPI user %s", row["temp_user_uuid"])
    if row["automatic"] and result.get("status") == "completed" and dpi_result_unavailable(result):
        asyncio.create_task(rotate_node_ip(request.app, row["node_uuid"]))
    return {"ok": True}


@app.get("/api/nodes/{node_uuid}/dpi")
async def dpi_history(node_uuid: str, _: str = Depends(require_user)):
    with closing(connect_db()) as db:
        rows = db.execute("SELECT id,target,location,provider_id,status,result,created_at,updated_at,kind,source FROM dpi_checks WHERE node_uuid=? ORDER BY id DESC LIMIT 200", (node_uuid,)).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        try:
            result = json.loads(row["result"] or "{}")
            if not isinstance(result, dict):
                raise ValueError("DPI result must be an object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "Invalid stored DPI result row_id=%s node_uuid=%s: %s",
                row["id"],
                node_uuid,
                exc,
            )
            result = {
                "status": "failed",
                "error": "Stored DPI result is invalid",
                "results": [],
            }
            item["status"] = "failed"
        item["target"] = (
            "[VLESS hidden]" if row["kind"] == "vless" else row["target"]
        )
        item["result"] = result
        history.append(item)
    return history


@app.get("/api/dpi/history")
async def dpi_history_all(_: str = Depends(require_user)):
    with closing(connect_db()) as db:
        rows = db.execute(
            "SELECT d.id,d.node_uuid,d.target,d.location,d.provider_id,"
            "d.status,d.result,d.created_at,d.updated_at,d.kind,d.source,"
            "n.name node_name,n.country_code node_country_code,"
            "n.address node_address FROM dpi_checks d JOIN nodes n "
            "ON n.uuid=d.node_uuid ORDER BY d.id DESC LIMIT 300"
        ).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        try:
            result = json.loads(row["result"] or "{}")
            if not isinstance(result, dict):
                raise ValueError("DPI result must be an object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "Invalid stored DPI result row_id=%s node_uuid=%s: %s",
                row["id"],
                row["node_uuid"],
                exc,
            )
            result = {
                "status": "failed",
                "error": "Stored DPI result is invalid",
                "results": [],
            }
            item["status"] = "failed"
        item["target"] = (
            "[VLESS hidden]" if row["kind"] == "vless" else row["target"]
        )
        item["result"] = result
        history.append(item)
    return history


@app.get("/api/dpi/overview")
async def dpi_overview(_: str = Depends(require_user)):
    if not dpi_is_configured():
        raise HTTPException(503, "DPI_API_KEY is not configured")
    try:
        overview = await create_dpi_client().overview()
        remote_checks = overview.get("checks", {})
        if isinstance(remote_checks, dict):
            remote_checks = next((remote_checks.get(key) for key in ("checks", "items", "results", "data") if isinstance(remote_checks.get(key), list)), [])
        if isinstance(remote_checks, list):
            asyncio.create_task(sync_dpi_billing(remote_checks))
        with closing(connect_db()) as db:
            rows = db.execute(
                "SELECT d.node_uuid,d.provider_id,d.target,d.kind,"
                "d.created_at,n.name,n.country_code,d.status,d.result "
                "FROM dpi_checks d JOIN nodes n ON n.uuid=d.node_uuid "
                "ORDER BY d.id DESC"
            ).fetchall()
        by_node: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = by_node.setdefault(row["node_uuid"], {"node_uuid": row["node_uuid"], "name": row["name"], "checks": 0, "completed": 0, "failed": 0, "spent": 0.0})
            item["checks"] += 1
            status_name = str(row["status"]).lower()
            if status_name == "completed": item["completed"] += 1
            if status_name in {"failed", "cancelled", "timeout"}: item["failed"] += 1
            result = json.loads(row["result"])
            item["spent"] += float(number_or_none(result.get("usd_cost", result.get("estimated_cost"))) or 0)
        local_by_provider = {
            int(row["provider_id"]): row
            for row in rows
            if row["provider_id"] is not None
        }
        if isinstance(remote_checks, list):
            for check in remote_checks:
                provider_id = int(
                    check.get("id") or check.get("check_id") or 0
                )
                local = local_by_provider.get(provider_id)
                if local:
                    check.update(
                        {
                            "node_uuid": local["node_uuid"],
                            "node_name": local["name"],
                            "node_country_code": local["country_code"],
                            "node_target": (
                                "[VLESS hidden]"
                                if local["kind"] == "vless"
                                else local["target"]
                            ),
                            "kind": local["kind"],
                            "created_at": (
                                check.get("created_at")
                                or local["created_at"]
                            ),
                        }
                    )
            overview["checks"] = remote_checks
        overview["stats"] = list(by_node.values())
        return overview
    except httpx.HTTPStatusError as exc:
        detail = f"DPI Checker returned HTTP {exc.response.status_code}"
        try:
            detail = exc.response.json().get("error", detail)
        except (ValueError, AttributeError):
            pass
        raise HTTPException(502, detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "DPI Checker is unavailable") from exc


@app.get("/internal/metrics")
async def metrics(credentials: HTTPBasicCredentials | None = Depends(security)):
    expected = settings.app_secret[:24]
    if not credentials or not hmac.compare_digest(credentials.username, "metrics") or not hmac.compare_digest(credentials.password, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
    with closing(connect_db()) as db:
        nodes = db.execute("SELECT n.uuid,n.name,s.is_down,(SELECT online FROM samples WHERE node_uuid=n.uuid ORDER BY id DESC LIMIT 1) online FROM nodes n LEFT JOIN node_state s ON s.node_uuid=n.uuid").fetchall()
    lines = ["# HELP remnadown_node_online Current online users", "# TYPE remnadown_node_online gauge"]
    for node in nodes:
        safe_name = str(node["name"]).replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'remnadown_node_online{{uuid="{node["uuid"]}",name="{safe_name}"}} {node["online"] or 0}')
        lines.append(f'remnadown_node_down{{uuid="{node["uuid"]}",name="{safe_name}"}} {node["is_down"] or 0}')
    return HTMLResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
