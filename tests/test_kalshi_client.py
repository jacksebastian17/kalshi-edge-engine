"""Tests for Kalshi API client."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi_edge_engine.kalshi.client import (
    KalshiAPIError,
    KalshiAuthError,
    KalshiClient,
)

# Mock RSA private key in PEM format for testing
MOCK_PEM_KEY = b"""-----BEGIN RSA PRIVATE KEY-----
MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu
KUpRKfFLfRYC9AIKjbJTWit+CqvjWYzvQwECAwEAAQJAIJLixBy2qpFoS4DSmoEm
o3qGy0t6z09AIJtH+5OeRV1be+N4cDYJKffGzDa88vQENZiRm0GRq6a+HPGQMd2k
TQIhAKMSvzIBnni7ot/OSie2TmJLY4SwTQAevXysE2RbFDYdAiEBCUEaRQnMnbp7
9mxDXDf6AU0cN/RPBjb9qSHDcWZHGzUCIG2Es59z8ugGrDY+pxLQnwfotadxd+Uy
v/Ow5T0q5gIJAiEAyS4RaI9YG8EWx/2w0T67ZUVAw8eOMB6BIUg0Xcu+3okCIBOs
/5OiPgoTdSy7bcF9IGpSE8ZgGKzgYQVZeN97YE00
-----END RSA PRIVATE KEY-----
"""


class TestKalshiClientInit:
    """Test KalshiClient initialization."""

    def test_init_with_env_vars(self) -> None:
        """Test initialization reads from environment variables."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
        ):
            mock_load.return_value = MagicMock()  # Mock private key object
            client = KalshiClient()
            assert client.api_key_id == "test_key_id"
            assert hasattr(client, "private_key")

    def test_init_with_env_base_url(self) -> None:
        """Test initialization reads base URL from environment."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                    "KALSHI_BASE_URL": "https://test.example.com",
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key
            client = KalshiClient()
            assert client.base_url == "https://test.example.com"

    def test_init_with_explicit_params(self) -> None:
        """Test initialization with explicit parameters."""
        with patch(
            "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
        ) as mock_load:
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key
            client = KalshiClient(
                api_key_id="explicit_key",
                api_secret=MOCK_PEM_KEY.decode("utf-8"),
                base_url="https://explicit.example.com",
            )
            assert client.api_key_id == "explicit_key"
            assert hasattr(client, "private_key")
            assert client.base_url == "https://explicit.example.com"

    def test_init_missing_credentials(self) -> None:
        """Test initialization fails when credentials are missing."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KalshiAuthError) as excinfo:
                KalshiClient()
            assert "KALSHI_API_KEY_ID" in str(excinfo.value)

    def test_init_default_base_url(self) -> None:
        """Test initialization uses default base URL."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key
            client = KalshiClient()
            assert client.base_url == "https://api.elections.kalshi.com/trade-api/v2"


class TestKalshiClientGetOrderbook:
    """Test KalshiClient.get_orderbook method."""

    @pytest.mark.asyncio
    async def test_get_orderbook_success(self) -> None:
        """Test successful orderbook fetch."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load_key,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load_key.return_value = mock_private_key

            client = KalshiClient()

            mock_orderbook = {
                "bid": 0.48,
                "ask": 0.51,
                "bid_size": 100,
                "ask_size": 150,
            }

            with patch("kalshi_edge_engine.kalshi.client.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = mock_orderbook
                mock_response.raise_for_status.return_value = None

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)

                mock_client_class.return_value = mock_client

                result = await client.get_orderbook("TRUMP-ELECTION")

                assert result == mock_orderbook
                mock_client.get.assert_called_once()
                call_kwargs = mock_client.get.call_args[1]
                # Check for RSA signature headers instead of basic auth
                assert "headers" in call_kwargs
                headers = call_kwargs["headers"]
                assert "KALSHI-ACCESS-KEY" in headers
                assert "KALSHI-ACCESS-TIMESTAMP" in headers
                assert "KALSHI-ACCESS-SIGNATURE" in headers
                assert call_kwargs["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_get_orderbook_invalid_ticker_type(self) -> None:
        """Test that invalid ticker type raises ValueError."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key
            client = KalshiClient()

            with pytest.raises(ValueError) as excinfo:
                await client.get_orderbook("")
            assert "Ticker must be a non-empty string" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_get_orderbook_http_status_error(self) -> None:
        """Test handling of HTTP status errors."""
        import httpx

        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key
            client = KalshiClient()

            with patch("kalshi_edge_engine.kalshi.client.httpx.AsyncClient") as mock_client_class:
                # Create a proper HTTPStatusError
                mock_request = MagicMock()
                mock_response = MagicMock()
                mock_response.status_code = 404

                error = httpx.HTTPStatusError(
                    "Not found",
                    request=mock_request,
                    response=mock_response,
                )
                mock_response.raise_for_status.side_effect = error

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)

                mock_client_class.return_value = mock_client

                with pytest.raises(KalshiAPIError) as excinfo:
                    await client.get_orderbook("INVALID-TICKER")
                assert "404" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_get_orderbook_request_error(self) -> None:
        """Test handling of network request errors."""
        import httpx

        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key
            client = KalshiClient()

            with patch("kalshi_edge_engine.kalshi.client.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)

                mock_client_class.return_value = mock_client

                with pytest.raises(KalshiAPIError) as excinfo:
                    await client.get_orderbook("TRUMP-ELECTION")
                assert "Request failed" in str(excinfo.value)


class TestKalshiClientPersistence:
    """Test persistent httpx.AsyncClient handling."""

    @pytest.mark.asyncio
    async def test_client_async_context_manager(self) -> None:
        """Test that KalshiClient works as async context manager."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
            patch("kalshi_edge_engine.kalshi.client.httpx.AsyncClient") as mock_client_class,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key

            mock_http_client = AsyncMock()
            mock_http_client.aclose = AsyncMock()
            mock_client_class.return_value = mock_http_client

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"test": "data"}
            mock_response.raise_for_status.return_value = None
            mock_http_client.get = AsyncMock(return_value=mock_response)

            async with KalshiClient() as client:
                assert client is not None
                # Use the client to trigger lazy initialization
                await client.get_orderbook("TEST")

            # After exiting context, aclose should have been called
            mock_http_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_aclose_method(self) -> None:
        """Test manual aclose() method."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
            patch("kalshi_edge_engine.kalshi.client.httpx.AsyncClient") as mock_client_class,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key

            mock_http_client = AsyncMock()
            mock_http_client.aclose = AsyncMock()
            mock_client_class.return_value = mock_http_client

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"test": "data"}
            mock_response.raise_for_status.return_value = None
            mock_http_client.get = AsyncMock(return_value=mock_response)

            client = KalshiClient()

            # Use client to initialize http client
            await client.get_orderbook("TEST")
            assert client._client is not None

            # Manually close
            await client.aclose()
            mock_http_client.aclose.assert_called_once()

            # Subsequent calls to aclose() should be safe
            await client.aclose()
            mock_http_client.aclose.assert_called_once()  # Still only called once

    @pytest.mark.asyncio
    async def test_client_reuses_http_client(self) -> None:
        """Test that KalshiClient reuses the same httpx.AsyncClient across calls."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
            patch("kalshi_edge_engine.kalshi.client.httpx.AsyncClient") as mock_client_class,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key

            # Track how many times AsyncClient is instantiated
            mock_http_client = AsyncMock()
            mock_client_class.return_value = mock_http_client

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"test": "data"}
            mock_response.raise_for_status.return_value = None
            mock_http_client.get = AsyncMock(return_value=mock_response)

            client = KalshiClient()

            # Make multiple requests
            await client.get_orderbook("TEST1")
            await client.get_orderbook("TEST2")
            await client.get_orderbook("TEST3")

            # AsyncClient should be instantiated only once
            mock_client_class.assert_called_once()


class TestKalshiClientSignaturePath:
    """Test request path derivation for signature."""

    @pytest.mark.asyncio
    async def test_signature_path_derived_from_url(self) -> None:
        """Test that signature path is derived from URL using urlparse."""
        with (
            patch.dict(
                os.environ,
                {
                    "KALSHI_API_KEY_ID": "test_key_id",
                    "KALSHI_API_SECRET": MOCK_PEM_KEY.decode("utf-8"),
                },
            ),
            patch(
                "kalshi_edge_engine.kalshi.client.serialization.load_pem_private_key"
            ) as mock_load,
        ):
            mock_private_key = MagicMock()
            mock_private_key.sign.return_value = b"mock_signature"
            mock_load.return_value = mock_private_key

            client = KalshiClient()

            # Patch the _sign_request to capture what path is used
            original_sign = client._sign_request
            captured_paths = []

            def capture_sign(timestamp_ms: int, method: str, path: str) -> str:
                captured_paths.append(path)
                return original_sign(timestamp_ms, method, path)

            client._sign_request = capture_sign  # type: ignore

            with patch("kalshi_edge_engine.kalshi.client.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"test": "data"}
                mock_response.raise_for_status.return_value = None

                mock_http_client = AsyncMock()
                mock_http_client.get = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_http_client

                await client.get_orderbook("TEST-TICKER")

                # Verify path was derived from URL correctly
                assert len(captured_paths) == 1
                path = captured_paths[0]
                # Path should be /trade-api/v2/markets/TEST-TICKER/orderbook (derived from URL)
                assert "TEST-TICKER" in path
                assert "orderbook" in path
                assert path.startswith("/")
