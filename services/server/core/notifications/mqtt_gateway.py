"""Authenticated HTTP client for the MQTT Gateway notification API."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlsplit

import httpx


class MqttGatewayClient:
    """Publish idempotent device events and read application-level receipts."""

    def __init__(self, address: str, signature_key: str, *, client: httpx.AsyncClient | None = None):
        if not isinstance(address, str) or not address.strip() or address.strip().lower() == "null":
            raise ValueError("MQTT Gateway management address is missing")
        if not isinstance(signature_key, str) or not signature_key.strip() or signature_key.strip().lower() == "null":
            raise ValueError("MQTT Gateway signature key is missing")
        address = address.strip()
        if "://" not in address:
            address = "http://" + address
        parsed = urlsplit(address)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MQTT Gateway management address must be an HTTP(S) origin")
        self._base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._signature_key = signature_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
        self._owns_client = client is None

    def _daily_tokens(self) -> tuple[str, ...]:
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        return tuple(
            hashlib.sha256(f"{day.isoformat()}{self._signature_key}".encode()).hexdigest()
            for day in (today, today - timedelta(days=1), today + timedelta(days=1))
        )

    async def _request(self, method: str, path: str, *, params=None, json_body=None):
        for token in self._daily_tokens():
            try:
                response = await self._client.request(
                    method,
                    self._base_url + path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError:
                return None
            if response.status_code == 401:
                continue
            if not 200 <= response.status_code < 300:
                return None
            try:
                body = response.json()
            except ValueError:
                return None
            if not isinstance(body, dict) or body.get("success", True) is False:
                return None
            return body
        return None

    async def publish(self, device_id: str, payload: dict) -> bool:
        body = await self._request(
            "POST",
            f"/api/notifications/{quote(device_id, safe='')}",
            json_body=payload,
        )
        if body is None:
            return False
        return body.get("accepted") is True

    async def receipt(self, device_id: str, reminder_id: str, revision: int):
        body = await self._request(
            "GET",
            f"/api/notifications/{quote(device_id, safe='')}/{quote(reminder_id, safe='')}/receipt",
            params={"revision": revision},
        )
        if body is None:
            return None
        data = body.get("data", body)
        if isinstance(data, dict) and "receipt" in data:
            data = data["receipt"]
        return data if isinstance(data, dict) else None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
