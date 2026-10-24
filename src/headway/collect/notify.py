"""Optional notifications through an ntfy topic (https://ntfy.sh)."""

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    """Posts short messages to an ntfy topic URL; does nothing if the URL is empty.

    Notification failures are logged and never interrupt collection.
    """

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        """Create a notifier using ``client``; ``url`` may be empty."""
        self._client = client
        self._url = url

    @property
    def enabled(self) -> bool:
        """True if a topic URL is configured."""
        return bool(self._url)

    async def send(self, message: str, *, title: str, priority: str = "default") -> None:
        """Send one message."""
        if not self.enabled:
            return
        try:
            response = await self._client.post(
                self._url,
                content=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority},
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("notification failed", extra={"error": type(exc).__name__})
