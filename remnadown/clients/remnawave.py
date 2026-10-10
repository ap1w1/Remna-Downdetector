from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from remnadown.config import Settings


class RemnawaveClient:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        permission_alerts: deque[dict[str, str]],
    ) -> None:
        self.logger = logger
        self.permission_alerts = permission_alerts
        headers = {
            "Authorization": f"Bearer {settings.remnawave_api_token}",
            "Accept": "application/json",
        }
        if settings.remnawave_caddy_token:
            headers["X-Api-Key"] = settings.remnawave_caddy_token
        self.client = httpx.AsyncClient(
            base_url=settings.remnawave_url.rstrip("/"),
            headers=headers,
            timeout=20,
            follow_redirects=False,
            event_hooks={"response": [self.audit_response]},
        )

    async def audit_response(self, response: httpx.Response) -> None:
        detail = ""
        if response.status_code >= 400:
            await response.aread()
            try:
                payload = response.json()
                detail = str(
                    payload.get("message")
                    or payload.get("error")
                    or payload.get("errors")
                    or ""
                )[:1000]
            except ValueError:
                detail = response.text[:1000]
        if response.status_code in {401, 403}:
            self.logger.error(
                "Remnawave API denied %s %s (HTTP %s): token lacks "
                "permission or is invalid. %s",
                response.request.method,
                response.request.url.path,
                response.status_code,
                detail,
            )
            value = (
                f"{time.time()}:{response.request.method}:"
                f"{response.request.url.path}"
            )
            self.permission_alerts.appendleft(
                {
                    "id": hashlib.sha256(value.encode()).hexdigest()[:12],
                    "message": (
                        "Remnawave: недостаточно прав для "
                        f"{response.request.method} {response.request.url.path} "
                        f"(HTTP {response.status_code}). {detail}"
                    ),
                }
            )
        elif response.status_code >= 400:
            self.logger.error(
                "Remnawave API error %s %s (HTTP %s): %s",
                response.request.method,
                response.request.url.path,
                response.status_code,
                detail,
            )

    async def nodes(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/nodes")
        response.raise_for_status()
        payload = response.json()
        data = payload.get("response", payload)
        if isinstance(data, dict):
            data = data.get("nodes", data.get("items", []))
        if not isinstance(data, list):
            raise ValueError("Unexpected Remnawave /api/nodes response")
        return data

    async def metrics(self) -> dict[str, dict[str, Any]]:
        for path in ("/api/system/nodes/metrics", "/api/nodes/metrics"):
            response = await self.client.get(path)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            payload = response.json().get("response", response.json())
            items = (
                payload.get("nodes", payload.get("items", []))
                if isinstance(payload, dict)
                else payload
            )
            return {
                str(item.get("nodeUuid") or item.get("uuid")): item
                for item in items or []
            }
        return {}

    async def panel_version(self) -> str:
        response = await self.client.get("/api/system/stats/recap")
        if response.status_code == 404:
            return "legacy"
        response.raise_for_status()
        payload = response.json().get("response", response.json())
        return str(
            payload.get("version")
            or payload.get("system", {}).get("version")
            or "latest"
        )

    async def user_by_username(self, username: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/users/by-username/{username}")
        response.raise_for_status()
        return response.json().get("response", response.json())

    async def create_dpi_user(
        self,
        node_uuid: str,
        selected_squads: list[str] | None = None,
        squad_catalog: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        identifier = secrets.token_hex(4)
        username = f"dpi_checker_{identifier}"
        internal_squads = squad_catalog
        if internal_squads is None:
            internal_squads = (await self.squads())["internal"]
        accessible_ids = [
            item["uuid"]
            for item in internal_squads
            if node_uuid in item["node_uuids"]
        ]
        squad_ids = [
            item for item in (selected_squads or accessible_ids)
            if item in accessible_ids
        ]
        if selected_squads and not squad_ids:
            raise ValueError(
                "Selected internal squads do not have access to this node"
            )
        payload = {
            "username": username,
            "expireAt": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
            "status": "ACTIVE",
            "trafficLimitStrategy": "NO_RESET",
            "trafficLimitBytes": 1073741824,
            "tag": "dpi//checker",
            "activeInternalSquads": squad_ids,
            "description": (
                f"dpi//checker · Temporary DPI checker for node {node_uuid}"
            ),
        }
        response = await self.client.post("/api/users", json=payload)
        if (
            response.status_code == 400
            and "Tag can only contain uppercase" in response.text
        ):
            payload["tag"] = "DPI_CHECKER"
            self.logger.warning(
                "Remnawave rejects tag dpi//checker; retrying temporary "
                "user %s with API-compatible tag DPI_CHECKER",
                username,
            )
            response = await self.client.post("/api/users", json=payload)
        response.raise_for_status()
        user = response.json().get("response", response.json())
        self.logger.info(
            "Created temporary Remnawave user %s (%s) requested_tag="
            "dpi//checker api_tag=%s node=%s squads=%s",
            username,
            user.get("uuid"),
            payload["tag"],
            node_uuid,
            ",".join(squad_ids),
        )
        return user

    @staticmethod
    def _subscription_links(content: str) -> list[str]:
        decoded = content.strip()
        if not decoded.startswith("vless://"):
            try:
                padding = "=" * (-len(decoded) % 4)
                decoded = base64.b64decode(decoded + padding).decode()
            except (ValueError, UnicodeDecodeError):
                return []
        return [
            line.strip()
            for line in decoded.splitlines()
            if line.strip().startswith("vless://")
        ]

    @staticmethod
    def _is_placeholder_vless(key: str) -> bool:
        parsed = urlsplit(key)
        label = unquote(parsed.fragment).casefold()
        return bool(
            parsed.username == "00000000-0000-0000-0000-000000000000"
            or parsed.hostname in {"0.0.0.0", "::"}
            or parsed.port == 1
            or "не поддерживается" in label
            or "not supported" in label
        )

    @staticmethod
    def _node_key_score(
        key: str,
        node_name: str,
        node_address: str,
        node_hostnames: tuple[str, ...] = (),
        node_labels: tuple[str, ...] = (),
    ) -> int:
        parsed = urlsplit(key)
        label = "".join(
            character.casefold()
            for character in unquote(parsed.fragment)
            if character.isalnum()
        )
        expected = "".join(
            character.casefold()
            for character in node_name
            if character.isalnum()
        )
        expected_labels = {
            "".join(
                character.casefold()
                for character in value
                if character.isalnum()
            )
            for value in node_labels
            if value
        }
        score = 0
        if parsed.hostname and any(
            parsed.hostname.casefold() == hostname.casefold()
            for hostname in node_hostnames
        ):
            score += 300
        if label and label in expected_labels:
            score += 250
        if node_address and parsed.hostname:
            if parsed.hostname.casefold() == node_address.casefold():
                score += 200
        if expected and label == expected:
            score += 150
        elif expected and expected in label:
            score += 100
        elif expected and label and label in expected:
            score += 80
        return score

    async def vless_key(
        self,
        user: dict[str, Any],
        node_name: str = "",
        node_address: str = "",
        node_hostnames: tuple[str, ...] = (),
        node_labels: tuple[str, ...] = (),
    ) -> str:
        subscription_url = str(
            user.get("subscriptionUrl") or user.get("subscription_url") or ""
        )
        if not subscription_url:
            raise ValueError("Remnawave did not return a subscription URL")
        fingerprint_source = str(user.get("uuid") or subscription_url)
        fingerprint = hashlib.sha256(fingerprint_source.encode()).hexdigest()
        headers = {
            "User-Agent": "Shadowrocket/2.2.60",
            "x-hwid": f"remnadown-{fingerprint[:32]}",
            "x-device-os": "Linux",
            "x-device-model": "RemnaDownDetector",
        }
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
        ) as client:
            response = await client.get(
                subscription_url,
                headers=headers,
            )
            response.raise_for_status()
        if response.headers.get("x-hwid-not-supported") == "true":
            raise ValueError(
                "Remnawave rejected the DPI checker HWID fingerprint"
            )
        links = [
            key
            for key in self._subscription_links(response.text)
            if not self._is_placeholder_vless(key)
        ]
        if not links:
            raise ValueError(
                "Remnawave returned no usable VLESS keys for this squad"
            )
        if not any((node_name, node_address, node_hostnames, node_labels)):
            return links[0]
        ranked = sorted(
            (
                (
                    self._node_key_score(
                        key,
                        node_name,
                        node_address,
                        node_hostnames,
                        node_labels,
                    ),
                    key,
                )
                for key in links
            ),
            reverse=True,
        )
        if not ranked or ranked[0][0] <= 0:
            raise ValueError("The Remnawave subscription contains no VLESS key")
        return ranked[0][1]

    async def temporary_vless(
        self,
        node_uuid: str,
        node_name: str,
        node_address: str,
        country_code: str = "",
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        squad_data, hosts = await asyncio.gather(
            self.squads(),
            self.hosts(),
        )
        squads = squad_data["internal"]
        candidates = [
            squad
            for squad in squads
            if node_uuid in squad.get("node_uuids", [])
        ]
        if not candidates:
            raise ValueError(
                "No internal Remnawave squad has access to this node"
            )

        linked_hosts = [
            host
            for host in hosts
            if not host.get("isDisabled", False)
            and node_uuid in {
                str(
                    item.get("uuid")
                    if isinstance(item, dict)
                    else item
                )
                for item in host.get("nodes", [])
            }
        ]
        node_hostnames = tuple(
            dict.fromkeys(
                str(host.get("address", "")).strip()
                for host in linked_hosts
                if host.get("address")
            )
        )
        node_labels = tuple(
            dict.fromkeys(
                str(host.get("remark", "")).strip()
                for host in linked_hosts
                if host.get("remark")
            )
        )
        if not linked_hosts:
            raise ValueError(
                f"Для ноды «{node_name}» не настроен активный Host "
                "в Remnawave. Привяжите к ноде VLESS/XHTTP/WS Host."
            )
        if all(
            "HYSTERIA" in str(host.get("remark", "")).upper()
            for host in linked_hosts
        ):
            raise ValueError(
                f"Для ноды «{node_name}» настроен только Hysteria Host. "
                "Для проверки по VLESS добавьте VLESS/XHTTP/WS Host."
            )

        aliases = {
            "DE": ("GERMANY", "FRANKFURT"),
            "FI": ("FINLAND",),
            "FR": ("FRANCE",),
            "NL": ("NETHERLAND",),
            "RU": ("RUSSIA",),
            "US": ("USA", "AMERICA"),
        }
        country_aliases = aliases.get(country_code.upper(), ())

        def candidate_rank(
            squad: dict[str, Any],
        ) -> tuple[int, int, str]:
            name = str(squad.get("name", "")).upper()
            country_match = int(
                any(alias in name for alias in country_aliases)
            )
            return (
                -country_match,
                len(squad.get("node_uuids", [])),
                name,
            )

        failures: list[str] = []
        for squad in sorted(candidates, key=candidate_rank):
            user = await self.create_dpi_user(
                node_uuid,
                [str(squad["uuid"])],
                squads,
            )
            user_uuid = str(user.get("uuid", ""))
            try:
                key = await self.vless_key(
                    user,
                    node_name,
                    node_address,
                    node_hostnames,
                    node_labels,
                )
            except Exception as exc:
                failures.append(f"{squad['name']}: {exc}")
                self.logger.warning(
                    "Temporary VLESS candidate rejected node=%s "
                    "squad=%s squad_uuid=%s user=%s error=%s",
                    node_uuid,
                    squad["name"],
                    squad["uuid"],
                    user.get("username", ""),
                    exc,
                )
                if user_uuid:
                    try:
                        await self.delete_user(user_uuid)
                    except Exception:
                        self.logger.exception(
                            "Failed to delete rejected temporary DPI user %s",
                            user_uuid,
                        )
                continue
            self.logger.info(
                "Selected internal squad for temporary VLESS node=%s "
                "squad=%s squad_uuid=%s user=%s",
                node_uuid,
                squad["name"],
                squad["uuid"],
                user.get("username", ""),
            )
            return key, user, squad

        self.logger.error(
            "No internal squad produced a usable VLESS key node=%s "
            "linked_hosts=%s failures=%s",
            node_uuid,
            len(linked_hosts),
            failures,
        )
        raise ValueError(
            f"Remnawave не вернул VLESS-ключ ноды «{node_name}». "
            "Проверьте, что её активный VLESS/XHTTP/WS Host доступен в "
            "выбранном internal squad."
        )

    async def delete_user(self, user_uuid: str) -> None:
        response = await self.client.delete(f"/api/users/{user_uuid}")
        if response.status_code != 404:
            response.raise_for_status()
        self.logger.info("Deleted temporary Remnawave DPI user %s", user_uuid)

    async def update_node_address(self, node_uuid: str, address: str) -> None:
        response = await self.client.patch(
            "/api/nodes",
            json={"uuid": node_uuid, "address": address},
        )
        response.raise_for_status()

    async def hosts(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/hosts")
        response.raise_for_status()
        payload = response.json().get("response", [])
        return payload if isinstance(payload, list) else []

    async def infra_providers(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/infra-billing/providers")
        response.raise_for_status()
        payload = response.json().get("response", response.json())
        return payload.get(
            "providers",
            payload if isinstance(payload, list) else [],
        )

    async def create_infra_provider(self, name: str) -> dict[str, Any]:
        payload = {"name": name}
        if name.upper() == "DPI//CHECKER":
            payload.update(
                {
                    "faviconLink": (
                        "https://dpichecker.st/public/web/img/favicon.ico"
                    ),
                    "loginUrl": "https://t.me/dpi_checker_robot",
                }
            )
        response = await self.client.post(
            "/api/infra-billing/providers",
            json=payload,
        )
        response.raise_for_status()
        return response.json().get("response", response.json())

    async def create_billing_record(
        self,
        provider_uuid: str,
        amount: float,
        billed_at: str,
    ) -> None:
        response = await self.client.post(
            "/api/infra-billing/history",
            json={
                "providerUuid": provider_uuid,
                "amount": amount,
                "billedAt": billed_at,
            },
        )
        response.raise_for_status()

    async def squads(self) -> dict[str, list[dict[str, Any]]]:
        internal_response, external_response = await asyncio.gather(
            self.client.get("/api/internal-squads"),
            self.client.get("/api/external-squads"),
        )
        internal_response.raise_for_status()
        external_response.raise_for_status()
        internal = internal_response.json().get("response", {}).get(
            "internalSquads", []
        )
        external = external_response.json().get("response", {}).get(
            "externalSquads", []
        )

        async def accessible(squad: dict[str, Any]) -> dict[str, Any]:
            response = await self.client.get(
                f"/api/internal-squads/{squad['uuid']}/accessible-nodes"
            )
            response.raise_for_status()
            nodes = response.json().get("response", {}).get(
                "accessibleNodes", []
            )
            return {
                "uuid": squad["uuid"],
                "name": squad["name"],
                "node_uuids": [item["uuid"] for item in nodes],
            }

        internal_with_nodes = (
            await asyncio.gather(*(accessible(item) for item in internal))
            if internal
            else []
        )
        return {
            "internal": list(internal_with_nodes),
            "external": [
                {
                    "uuid": item["uuid"],
                    "name": item["name"],
                    "members": item.get("info", {}).get("membersCount", 0),
                }
                for item in external
            ],
        }

    async def close(self) -> None:
        await self.client.aclose()
