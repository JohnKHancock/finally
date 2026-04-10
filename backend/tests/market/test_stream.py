"""Tests for SSE streaming endpoint."""

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


def _make_mock_request(disconnects_after: int = 3) -> MagicMock:
    """Build a mock FastAPI Request that disconnects after N checks."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    # Returns False (connected) N-1 times, then True (disconnected)
    side_effects = [False] * (disconnects_after - 1) + [True]
    request.is_disconnected = AsyncMock(side_effect=side_effects)
    return request


async def _collect_events(
    cache: PriceCache,
    request: MagicMock,
    interval: float = 0.01,
    max_events: int = 20,
) -> list[str]:
    """Collect up to max_events from _generate_events."""
    events = []
    async for event in _generate_events(cache, request, interval=interval):
        events.append(event)
        if len(events) >= max_events:
            break
    return events


class TestGenerateEventsRetry:
    """Tests for the retry header emitted at startup."""

    @pytest.mark.asyncio
    async def test_first_event_is_retry_header(self):
        cache = PriceCache()
        request = _make_mock_request(disconnects_after=1)
        events = await _collect_events(cache, request)
        assert events[0] == "retry: 1000\n\n"

    @pytest.mark.asyncio
    async def test_retry_header_always_first(self):
        """Even when cache has data, retry is emitted before any data event."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        request = _make_mock_request(disconnects_after=2)
        events = await _collect_events(cache, request)
        assert events[0] == "retry: 1000\n\n"


class TestGenerateEventsData:
    """Tests for data events sent after retry."""

    @pytest.mark.asyncio
    async def test_data_event_emitted_when_cache_has_data(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        request = _make_mock_request(disconnects_after=3)

        events = await _collect_events(cache, request, interval=0.001)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

    @pytest.mark.asyncio
    async def test_data_event_contains_correct_ticker(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        request = _make_mock_request(disconnects_after=3)

        events = await _collect_events(cache, request, interval=0.001)
        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        payload = json.loads(data_events[0][len("data: "):-2])  # Strip "data: " prefix and "\n\n"
        assert "AAPL" in payload
        assert payload["AAPL"]["price"] == 190.50

    @pytest.mark.asyncio
    async def test_data_event_contains_all_tracked_tickers(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.update("MSFT", 420.00)
        request = _make_mock_request(disconnects_after=3)

        events = await _collect_events(cache, request, interval=0.001)
        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        payload = json.loads(data_events[0][len("data: "):-2])
        assert set(payload.keys()) == {"AAPL", "GOOGL", "MSFT"}

    @pytest.mark.asyncio
    async def test_data_event_format(self):
        """Each data event must end with double newline (SSE spec)."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        request = _make_mock_request(disconnects_after=3)

        events = await _collect_events(cache, request, interval=0.001)
        for event in events:
            assert event.endswith("\n\n"), f"Event does not end with \\n\\n: {event!r}"

    @pytest.mark.asyncio
    async def test_no_data_event_when_cache_empty(self):
        """When cache is empty, no data events should be emitted."""
        cache = PriceCache()
        request = _make_mock_request(disconnects_after=3)

        events = await _collect_events(cache, request, interval=0.001)
        data_events = [e for e in events if e.startswith("data:")]
        assert data_events == []

    @pytest.mark.asyncio
    async def test_data_event_json_has_required_fields(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        request = _make_mock_request(disconnects_after=3)

        events = await _collect_events(cache, request, interval=0.001)
        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        payload = json.loads(data_events[0][len("data: "):-2])
        aapl = payload["AAPL"]
        required_keys = {"ticker", "price", "previous_price", "timestamp", "change", "change_percent", "direction"}
        assert set(aapl.keys()) == required_keys


class TestGenerateEventsVersionDetection:
    """Tests for the version-based change detection."""

    @pytest.mark.asyncio
    async def test_duplicate_events_not_sent_when_version_unchanged(self):
        """If version hasn't changed between polls, no duplicate data events emitted."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        # 5 polls: the version never changes after initial seed
        request = _make_mock_request(disconnects_after=6)
        events = await _collect_events(cache, request, interval=0.001, max_events=10)

        data_events = [e for e in events if e.startswith("data:")]
        # Should only send once (when version changes from -1 to 1)
        assert len(data_events) == 1

    @pytest.mark.asyncio
    async def test_new_event_sent_when_price_updates(self):
        """When cache version changes, a new data event should be sent."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        request = _make_mock_request(disconnects_after=10)
        events = []

        async def generate_and_update():
            count = 0
            async for event in _generate_events(cache, request, interval=0.005):
                events.append(event)
                count += 1
                if count == 2:
                    # After first data event, update the price
                    cache.update("AAPL", 191.00)
                if count >= 4:
                    break

        await generate_and_update()

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 2


class TestGenerateEventsDisconnect:
    """Tests for client disconnect handling."""

    @pytest.mark.asyncio
    async def test_stops_when_client_disconnects(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        # Disconnect immediately after retry header
        request = _make_mock_request(disconnects_after=1)

        events = await _collect_events(cache, request, interval=0.001)

        # Should stop after retry header (disconnect detected before any data poll)
        assert events == ["retry: 1000\n\n"]

    @pytest.mark.asyncio
    async def test_no_client_host_handled(self):
        """Handles missing request.client gracefully."""
        cache = PriceCache()
        request = MagicMock()
        request.client = None  # No client info
        request.is_disconnected = AsyncMock(side_effect=[False, True])

        events = []
        async for event in _generate_events(cache, request, interval=0.001):
            events.append(event)

        assert events[0] == "retry: 1000\n\n"


class TestCreateStreamRouter:
    """Tests for the create_stream_router factory."""

    def test_returns_api_router(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        assert isinstance(router, APIRouter)

    def test_router_has_prices_route(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        route_paths = [r.path for r in router.routes]
        assert any("prices" in p for p in route_paths), f"No prices route found: {route_paths}"

    def test_creates_fresh_router_each_call(self):
        """Each call creates a new router to avoid double-registration."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)
        assert router1 is not router2

    def test_each_router_has_exactly_one_prices_route(self):
        """Route should not accumulate on repeated calls."""
        cache = PriceCache()
        router = create_stream_router(cache)
        prices_routes = [r for r in router.routes if r.path == "/api/stream/prices"]
        assert len(prices_routes) == 1

    def test_router_prefix(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        assert router.prefix == "/api/stream"
