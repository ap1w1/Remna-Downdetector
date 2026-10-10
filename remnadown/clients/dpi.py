from __future__ import annotations

import asyncio
import secrets
from typing import Any

import httpx


class DpiClient:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        callback_url: str = "",
    ) -> None:
        if not api_key:
            raise ValueError("DPI_API_KEY is not configured")
        self.api_url = api_url
        self.api_key = api_key
        self.callback_url = callback_url

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_url,
            headers={"X-API-Key": self.api_key},
            timeout=130,
        )

    async def optimal_pops(
        self,
        client: httpx.AsyncClient,
        location: str,
        pop_ids: list[int] | None,
    ) -> list[int]:
        available_response = await client.get(
            "/pops",
            params={"location": location, "healthy": "true"},
        )
        available_response.raise_for_status()
        available_payload = available_response.json()
        available_items = (
            available_payload
            if isinstance(available_payload, list)
            else available_payload.get("pops", [])
        )
        available_ids = {
            int(item["id"])
            for item in available_items
            if item.get("id") is not None
        }
        selected = [
            int(pop_id)
            for pop_id in (pop_ids or [])
            if int(pop_id) in available_ids
        ]
        if selected:
            return selected
        response = await client.get(
            "/pops/optimal",
            params={"location": location},
        )
        response.raise_for_status()
        payload = response.json()
        selected = [
            int(pop_id)
            for pop_id in payload.get("pop_ids", [])
            if int(pop_id) in available_ids
        ] or [
            int(item["id"])
            for item in payload.get("pops", [])
            if int(item["id"]) in available_ids
        ]
        if not selected:
            raise ValueError("DPI Checker returned no available PoPs")
        return selected

    async def post_check(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await client.post(
            path,
            headers={"Idempotency-Key": secrets.token_hex(16)},
            json=payload,
        )
        if response.status_code == 400 and "pop_ids" in response.text:
            payload["pop_ids"] = await self.optimal_pops(
                client,
                str(payload["location"]),
                None,
            )
            response = await client.post(
                path,
                headers={"Idempotency-Key": secrets.token_hex(16)},
                json=payload,
            )
        response.raise_for_status()
        return response.json()

    async def start_ip(
        self,
        target: str,
        location: str = "russia",
        pop_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        async with self.client() as client:
            selected_pops = await self.optimal_pops(
                client,
                location,
                pop_ids,
            )
            payload: dict[str, Any] = {
                "location": location,
                "pop_ids": selected_pops,
                "resources": [target],
            }
            if self.callback_url:
                payload["callback_url"] = self.callback_url
            return await self.post_check(client, "/checks/ip", payload)

    async def start_vpn(
        self,
        key: str,
        location: str = "russia",
        pop_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        key = key.strip()
        if not key.lower().startswith("vless://"):
            raise ValueError(
                "DPI Checker requires a VLESS key, not a subscription URL"
            )
        async with self.client() as client:
            selected_pops = await self.optimal_pops(
                client,
                location,
                pop_ids,
            )
            payload: dict[str, Any] = {
                "location": location,
                "pop_ids": selected_pops,
                "keys": [key],
            }
            if self.callback_url:
                payload["callback_url"] = self.callback_url
            return await self.post_check(client, "/checks/vpn", payload)

    async def overview(self) -> dict[str, Any]:
        async with self.client() as client:
            profile, checks, pops = await asyncio.gather(
                client.get("/profile"),
                client.get(
                    "/checks",
                    params={"kind": "check", "limit": 100, "offset": 0},
                ),
                client.get(
                    "/pops",
                    params={"location": "russia", "healthy": "true"},
                ),
            )
            profile.raise_for_status()
            checks.raise_for_status()
            pops.raise_for_status()
            return {
                "profile": profile.json(),
                "checks": checks.json(),
                "pops": pops.json(),
            }

    async def profile(self) -> dict[str, Any]:
        async with self.client() as client:
            response = await client.get("/profile")
            response.raise_for_status()
            return response.json()

    async def get_check(self, check_id: int) -> dict[str, Any]:
        async with self.client() as client:
            response = await client.get(f"/checks/{check_id}")
            response.raise_for_status()
            return response.json()

    async def wait_check(self, check_id: int) -> dict[str, Any]:
        async with self.client() as client:
            response = await client.get(
                f"/checks/{check_id}/wait",
                params={"timeout": 60},
            )
            response.raise_for_status()
            return response.json()
