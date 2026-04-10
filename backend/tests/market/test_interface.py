"""Tests for the MarketDataSource abstract interface."""

import pytest

from app.market.cache import PriceCache
from app.market.interface import MarketDataSource
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


class TestMarketDataSourceABC:
    """Tests for the abstract base class contract."""

    def test_cannot_instantiate_abstract_class(self):
        with pytest.raises(TypeError):
            MarketDataSource()

    def test_simulator_is_market_data_source(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache)
        assert isinstance(source, MarketDataSource)

    def test_massive_is_market_data_source(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache)
        assert isinstance(source, MarketDataSource)

    def test_abstract_methods_defined(self):
        """MarketDataSource ABC declares the required abstract methods."""
        abstract_methods = MarketDataSource.__abstractmethods__
        assert "start" in abstract_methods
        assert "stop" in abstract_methods
        assert "add_ticker" in abstract_methods
        assert "remove_ticker" in abstract_methods
        assert "get_tickers" in abstract_methods

    def test_incomplete_implementation_raises(self):
        """A class that doesn't implement all abstract methods cannot be instantiated."""

        class IncompleteSource(MarketDataSource):
            async def start(self, tickers):
                pass
            # Missing: stop, add_ticker, remove_ticker, get_tickers

        with pytest.raises(TypeError):
            IncompleteSource()

    def test_complete_implementation_can_instantiate(self):
        """A class that implements all abstract methods can be instantiated."""

        class MinimalSource(MarketDataSource):
            async def start(self, tickers):
                pass

            async def stop(self):
                pass

            async def add_ticker(self, ticker):
                pass

            async def remove_ticker(self, ticker):
                pass

            def get_tickers(self):
                return []

        source = MinimalSource()
        assert isinstance(source, MarketDataSource)


class TestMarketDataSourceInterfaceCompliance:
    """Tests that both implementations satisfy the interface contract."""

    @pytest.mark.asyncio
    async def test_simulator_start_stop(self):
        cache = PriceCache()
        source: MarketDataSource = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.stop()

    @pytest.mark.asyncio
    async def test_simulator_add_remove_ticker(self):
        cache = PriceCache()
        source: MarketDataSource = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()

        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()

        await source.stop()

    def test_massive_get_tickers_returns_list(self):
        cache = PriceCache()
        source: MarketDataSource = MassiveDataSource(api_key="test", price_cache=cache)
        result = source.get_tickers()
        assert isinstance(result, list)

    def test_simulator_get_tickers_returns_list(self):
        cache = PriceCache()
        source: MarketDataSource = SimulatorDataSource(price_cache=cache)
        result = source.get_tickers()
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_both_sources_add_ticker_is_async(self):
        """add_ticker must be awaitable on both implementations."""
        cache = PriceCache()
        sim_source: MarketDataSource = SimulatorDataSource(price_cache=cache)
        massive_source: MarketDataSource = MassiveDataSource(api_key="test", price_cache=cache)

        # Just verify they're coroutines (awaitable)
        import asyncio
        assert asyncio.iscoroutinefunction(sim_source.add_ticker)
        assert asyncio.iscoroutinefunction(massive_source.add_ticker)

    @pytest.mark.asyncio
    async def test_both_sources_remove_ticker_is_async(self):
        cache = PriceCache()
        sim_source: MarketDataSource = SimulatorDataSource(price_cache=cache)
        massive_source: MarketDataSource = MassiveDataSource(api_key="test", price_cache=cache)

        import asyncio
        assert asyncio.iscoroutinefunction(sim_source.remove_ticker)
        assert asyncio.iscoroutinefunction(massive_source.remove_ticker)
