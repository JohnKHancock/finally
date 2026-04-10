"""Tests for market data source factory."""

import os
from unittest.mock import patch

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


class TestCreateMarketDataSource:
    """Tests for the create_market_data_source factory function."""

    def test_creates_simulator_when_no_api_key(self):
        cache = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_creates_simulator_when_api_key_empty(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": ""}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_creates_simulator_when_api_key_whitespace(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "   "}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_creates_massive_when_api_key_set(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key"}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveDataSource)

    def test_massive_receives_api_key(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "my-secret-key"}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveDataSource)
        assert source._api_key == "my-secret-key"

    def test_simulator_receives_cache(self):
        cache = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)
        assert source._cache is cache

    def test_massive_receives_cache(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key"}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveDataSource)
        assert source._cache is cache

    def test_different_caches_produce_independent_sources(self):
        cache1 = PriceCache()
        cache2 = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            source1 = create_market_data_source(cache1)
            source2 = create_market_data_source(cache2)
        assert source1._cache is not source2._cache

    def test_simulator_has_default_interval(self):
        cache = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)
        assert source._interval == 0.5

    def test_massive_has_default_interval(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key"}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveDataSource)
        assert source._interval == 15.0

    def test_returns_unstarted_source(self):
        """Factory returns an unstarted source; caller must call await source.start()."""
        cache = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)
        assert source._task is None  # Not yet started
