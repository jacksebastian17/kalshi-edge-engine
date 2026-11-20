"""Kalshi API client."""

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)


class KalshiClientError(Exception):
    """Base exception for KalshiClient errors."""

    pass


class KalshiAuthError(KalshiClientError):
    """Raised when authentication credentials are missing or invalid."""

    pass


class KalshiAPIError(KalshiClientError):
    """Raised when the Kalshi API returns an error."""

    pass


class KalshiClient:
    """Client for interacting with Kalshi API using RSA signature authentication.

    Supports connection pooling via persistent httpx.AsyncClient.
    Use as async context manager or manually call aclose() to clean up.

    Reads credentials from environment variables:
    - KALSHI_API_KEY_ID: API key ID (UUID)
    - KALSHI_API_KEY_FILE: Path to RSA private key PEM file (preferred)
    - KALSHI_API_SECRET: RSA private key PEM string (fallback, inline)
    - KALSHI_BASE_URL: Base URL (optional, defaults to elections API)
    """

    DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    api_key_id: str
    private_key: rsa.RSAPrivateKey
    base_url: str
    _client: httpx.AsyncClient | None

    def __init__(
        self,
        api_key_id: str | None = None,
        api_key_file: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Initialize the Kalshi client.

        Args:
            api_key_id: API key ID (reads from KALSHI_API_KEY_ID if not provided).
            api_key_file: Path to PEM file (reads from KALSHI_API_KEY_FILE if not provided).
            api_secret: RSA private key PEM string (reads from KALSHI_API_SECRET if not provided).
            base_url: Base URL (reads from KALSHI_BASE_URL or uses default).

        Raises:
            KalshiAuthError: If credentials cannot be found or private key is invalid.
        """
        api_key_id_value = api_key_id or os.getenv("KALSHI_API_KEY_ID")
        key_file_path = api_key_file or os.getenv("KALSHI_API_KEY_FILE")
        api_secret_str = api_secret or os.getenv("KALSHI_API_SECRET")
        base_url_value = base_url or os.getenv("KALSHI_BASE_URL", self.DEFAULT_BASE_URL)

        if not api_key_id_value:
            raise KalshiAuthError(
                "Kalshi API key ID not found. Set KALSHI_API_KEY_ID environment variable."
            )

        # Type narrowing for mypy - base_url_value always has default value
        assert api_key_id_value is not None  # Already checked above
        assert base_url_value is not None  # Has default value

        self.api_key_id = api_key_id_value
        self.base_url = base_url_value
        self._client = None

        # Load RSA private key from file (preferred) or inline string
        try:
            if key_file_path:
                # Load from file
                key_path = Path(key_file_path)
                if not key_path.is_absolute():
                    # Resolve relative to project root (where .env is)
                    key_path = Path.cwd() / key_path
                if not key_path.exists():
                    raise FileNotFoundError(f"Key file not found: {key_path}")
                with open(key_path, "rb") as f:
                    key_data = f.read()
                self.private_key = serialization.load_pem_private_key(  # type: ignore[assignment]
                    key_data,
                    password=None,
                )
            elif api_secret_str:
                # Load from inline string
                self.private_key = serialization.load_pem_private_key(  # type: ignore[assignment]
                    api_secret_str.encode("utf-8"),
                    password=None,
                )
            else:
                raise KalshiAuthError(
                    "Kalshi private key not found. "
                    "Set KALSHI_API_KEY_FILE or KALSHI_API_SECRET environment variable."
                )
        except Exception as e:
            raise KalshiAuthError(f"Failed to load RSA private key: {e}") from e

    async def __aenter__(self) -> "KalshiClient":
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit async context manager and close underlying client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx.AsyncClient to release connections.

        Safe to call multiple times.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize or return persistent httpx.AsyncClient.

        Returns:
            Persistent AsyncClient for connection pooling.
        """
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    def _sign_request(self, timestamp_ms: int, method: str, path: str) -> str:
        """Create RSA-PSS signature for request authentication.

        Args:
            timestamp_ms: Current timestamp in milliseconds.
            method: HTTP method (GET, POST, etc.).
            path: Request path (e.g., /trade-api/v2/markets/TICK/orderbook).

        Returns:
            Base64-encoded signature string.
        """
        message = f"{timestamp_ms}{method}{path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """Fetch orderbook for a given ticker using RSA signature authentication.

        Args:
            ticker: Market ticker symbol (e.g., 'KXNBAGAME-26MAR01SASNYK').

        Returns:
            Orderbook data from Kalshi API.

        Raises:
            KalshiAPIError: If the API request fails.
        """
        if not ticker or not isinstance(ticker, str):
            raise ValueError("Ticker must be a non-empty string")

        url = f"{self.base_url}/markets/{ticker}/orderbook"
        # Derive path from URL to ensure signature matches actual request
        parsed_url = urlparse(url)
        path = parsed_url.path
        method = "GET"

        # Generate signature
        timestamp_ms = int(time.time() * 1000)
        signature = self._sign_request(timestamp_ms, method, path)

        headers: dict[str, str] = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

        try:
            client = await self._get_client()
            response = await client.get(
                url,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logger.info(
                "orderbook_fetch_success",
                extra={
                    "ticker": ticker,
                    "url": url,
                    "status_code": response.status_code,
                },
            )
            return data
        except httpx.HTTPStatusError as e:
            logger.error(
                "orderbook_fetch_status_error",
                extra={
                    "ticker": ticker,
                    "status_code": e.response.status_code,
                    "error": str(e),
                },
            )
            raise KalshiAPIError(f"API error for {ticker}: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(
                "orderbook_fetch_request_error",
                extra={
                    "ticker": ticker,
                    "error": str(e),
                },
            )
            raise KalshiAPIError(f"Request failed for {ticker}: {str(e)}") from e

    async def _authenticated_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to Kalshi API.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: Request path (e.g., /series).
            params: Optional query parameters.

        Returns:
            JSON response from Kalshi API.

        Raises:
            KalshiAPIError: If the API request fails.
        """
        url = f"{self.base_url}{path}"
        # Derive path from URL to ensure signature matches actual request
        parsed_url = urlparse(url)
        sign_path = parsed_url.path
        timestamp_ms = int(time.time() * 1000)
        signature = self._sign_request(timestamp_ms, method, sign_path)

        headers: dict[str, str] = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

        try:
            client = await self._get_client()
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=10.0,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data
        except httpx.HTTPStatusError as e:
            logger.error(
                "api_request_status_error",
                extra={
                    "method": method,
                    "path": path,
                    "status_code": e.response.status_code,
                    "error": str(e),
                },
            )
            raise KalshiAPIError(f"API error {method} {path}: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(
                "api_request_error",
                extra={
                    "method": method,
                    "path": path,
                    "error": str(e),
                },
            )
            raise KalshiAPIError(f"Request failed {method} {path}: {str(e)}") from e

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """Read market metadata, with a historical lookup after an HTTP 404."""
        market_path = f"/markets/{quote(ticker, safe='')}"
        try:
            data = await self._authenticated_request("GET", market_path)
        except KalshiAPIError as exc:
            cause = exc.__cause__
            if not isinstance(cause, httpx.HTTPStatusError) or cause.response.status_code != 404:
                raise
            data = await self._authenticated_request("GET", f"/historical{market_path}")
        return self._validated_record(data, "market", "ticker", ticker)

    async def get_event(self, ticker: str) -> dict[str, Any]:
        data = await self._authenticated_request("GET", f"/events/{quote(ticker, safe='')}")
        return self._validated_record(data, "event", "event_ticker", ticker)

    async def get_series_details(self, ticker: str) -> dict[str, Any]:
        data = await self._authenticated_request("GET", f"/series/{quote(ticker, safe='')}")
        return self._validated_record(data, "series", "ticker", ticker)

    @staticmethod
    def _validated_record(
        data: dict[str, Any], record_type: str, ticker_key: str, ticker: str
    ) -> dict[str, Any]:
        record = data.get(record_type)
        if not isinstance(record, dict) or record.get(ticker_key) != ticker:
            raise KalshiAPIError(f"Invalid {record_type} response for {ticker}")
        return record

    async def get_series(
        self,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch series (event groups) from Kalshi API.

        Args:
            limit: Maximum number of series to return (default 200).
            cursor: Pagination cursor from previous response (optional).

        Returns:
            Response containing series list and optional cursor.
            Format: {"series": [...], "cursor": "..."}

        Raises:
            KalshiAPIError: If the API request fails.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor

        return await self._authenticated_request("GET", "/series", params)

    async def get_events(
        self,
        series_ticker: str | None = None,
        status: str | None = None,
        with_nested_markets: bool = False,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch events from Kalshi API.

        Args:
            series_ticker: Filter by series ticker (optional).
            status: Filter by status (e.g., 'open') (optional).
            with_nested_markets: Include nested market data (optional).
            limit: Maximum number of events to return (default 200).
            cursor: Pagination cursor from previous response (optional).

        Returns:
            Response containing events list and optional cursor.
            Format: {"events": [...], "cursor": "..."}

        Raises:
            KalshiAPIError: If the API request fails.
        """
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        if cursor:
            params["cursor"] = cursor

        return await self._authenticated_request("GET", "/events", params)

    async def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch markets from Kalshi API.

        Args:
            series_ticker: Filter by series ticker (optional).
            event_ticker: Filter by event ticker (optional).
            status: Filter by status (e.g., 'open') (optional).
            min_close_ts: Minimum close timestamp filter (optional).
            max_close_ts: Maximum close timestamp filter (optional).
            limit: Maximum number of markets to return (default 200).
            cursor: Pagination cursor from previous response (optional).

        Returns:
            Response containing markets list and optional cursor.
            Format: {"markets": [...], "cursor": "..."}

        Raises:
            KalshiAPIError: If the API request fails.
        """
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        if cursor:
            params["cursor"] = cursor

        return await self._authenticated_request("GET", "/markets", params)
