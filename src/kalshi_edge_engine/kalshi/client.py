"""Kalshi API client."""

from typing import Any


class KalshiClient:
    """Client for interacting with Kalshi API.

    Currently a stub implementation. Real authentication and API calls
    will be implemented in future iterations.
    """

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        """Initialize the Kalshi client.

        Args:
            api_key: Kalshi API key (optional for stub version).
            api_secret: Kalshi API secret (optional for stub version).
        """
        self.api_key = api_key
        self.api_secret = api_secret

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """Fetch orderbook for a given ticker.

        Args:
            ticker: Market ticker symbol.

        Returns:
            Orderbook data (stub implementation).

        Raises:
            NotImplementedError: This is a stub and is not yet implemented.
        """
        raise NotImplementedError("get_orderbook is not yet implemented")
