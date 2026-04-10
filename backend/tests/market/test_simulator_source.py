"""Integration tests for SimulatorDataSource."""

import asyncio

import pytest

from app.market.cache import PriceCache
from app.market.seed_prices import SEED_PRICES
from app.market.simulator import GBMSimulator, SimulatorDataSource


DEFAULT_TICKERS = list(SEED_PRICES.keys())


@pytest.mark.asyncio
class TestSimulatorDataSourceLifecycle:
    """Tests for start/stop lifecycle."""

    async def test_start_populates_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None

        await source.stop()

    async def test_start_all_10_default_tickers(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.5)
        await source.start(DEFAULT_TICKERS)

        for ticker in DEFAULT_TICKERS:
            assert cache.get(ticker) is not None, f"{ticker} not in cache after start"
            assert cache.get_price(ticker) > 0

        await source.stop()

    async def test_stop_is_idempotent(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()  # Second call should not raise

    async def test_stop_cancels_background_task(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        task = source._task
        assert task is not None
        assert not task.done()

        await source.stop()

        assert source._task is None
        assert task.done()

    async def test_empty_start(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start([])
        assert len(cache) == 0
        assert source.get_tickers() == []
        await source.stop()

    async def test_task_named_simulator_loop(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        assert source._task.get_name() == "simulator-loop"
        await source.stop()


@pytest.mark.asyncio
class TestSimulatorDataSourceUpdates:
    """Tests for live price update behavior."""

    async def test_prices_update_over_time(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])

        initial_version = cache.version
        await asyncio.sleep(0.3)

        assert cache.version > initial_version

        await source.stop()

    async def test_custom_update_interval_fast(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
        await source.start(["AAPL"])

        initial_version = cache.version
        await asyncio.sleep(0.08)

        assert cache.version > initial_version + 3

        await source.stop()

    async def test_exception_resilience(self):
        """Simulator loop continues after errors."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])

        await asyncio.sleep(0.15)

        assert source._task is not None
        assert not source._task.done()

        await source.stop()


@pytest.mark.asyncio
class TestSimulatorDataSourceWatchlist:
    """Tests for dynamic ticker management."""

    async def test_add_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")

        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None

        await source.stop()

    async def test_add_ticker_seeds_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=10.0)  # Slow loop
        await source.start(["AAPL"])

        await source.add_ticker("NVDA")

        # Cache should be seeded immediately, not waiting for next loop tick
        assert cache.get_price("NVDA") is not None
        assert cache.get_price("NVDA") > 0

        await source.stop()

    async def test_add_duplicate_ticker_is_noop(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("AAPL")

        assert source.get_tickers().count("AAPL") == 1

        await source.stop()

    async def test_remove_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "TSLA"])

        await source.remove_ticker("TSLA")

        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

        await source.stop()

    async def test_get_tickers_returns_initial_list(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        assert set(source.get_tickers()) == {"AAPL", "GOOGL"}

        await source.stop()

    async def test_get_tickers_before_start(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache)
        assert source.get_tickers() == []

    async def test_custom_event_probability(self):
        """High event probability still starts and stops cleanly."""
        cache = PriceCache()
        source = SimulatorDataSource(
            price_cache=cache, update_interval=0.05, event_probability=1.0
        )
        await source.start(["AAPL"])
        await asyncio.sleep(0.1)
        await source.stop()
