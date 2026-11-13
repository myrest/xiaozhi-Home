"""Single-flight authoritative snapshot delivery to the private notification API."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .store import Store


logger = logging.getLogger(__name__)


class Scheduler:
    """Send pending snapshots once a second; outages leave durable state pending."""

    def __init__(self, store: Store, notification_url: str, token: str, *,
                 client: httpx.AsyncClient | None = None, interval: float = 1.0):
        self.store = store
        self.notification_url = notification_url
        self._token = token
        self._client = client
        self._owns_client = client is None
        self._interval = interval
        self._sync_lock = asyncio.Lock()

    async def sync_once(self) -> None:
        """Send one full snapshot, accepting only outcomes for that exact snapshot."""
        async with self._sync_lock:
            try:
                snapshot = await asyncio.to_thread(self.store.snapshot)
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=5.0)
                response = await self._client.post(
                    self.notification_url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    json=snapshot, timeout=5.0,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or not isinstance(result.get("outcomes"), list):
                    logger.warning("Reminder notification response has invalid outcomes")
                    return
                await asyncio.to_thread(self.store.apply_outcomes, result["outcomes"], snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                # URLs, bearer tokens and remote exception text are private.
                logger.warning("Reminder notification sync failed; retrying on next tick")

    async def run(self) -> None:
        """Maintain one in-flight request, with a five-second HTTP timeout."""
        try:
            while True:
                started = time.monotonic()
                await self.sync_once()
                await asyncio.sleep(max(0, self._interval - (time.monotonic() - started)))
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
