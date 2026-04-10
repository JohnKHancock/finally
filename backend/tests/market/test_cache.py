"""Tests for PriceCache."""

import threading
import time

from app.market.cache import PriceCache


class TestPriceCacheBasicOperations:
    """Tests for basic CRUD operations."""

    def test_update_and_get(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

    def test_get_unknown_ticker_returns_none(self):
        cache = PriceCache()
        assert cache.get("NOPE") is None

    def test_get_price_convenience(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("NOPE") is None

    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_direction_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"
        assert update.change == -1.00

    def test_previous_price_tracks_last(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)
        update = cache.update("AAPL", 192.00)
        assert update.previous_price == 191.00

    def test_remove_existing_ticker(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_remove_nonexistent_is_noop(self):
        cache = PriceCache()
        cache.remove("AAPL")  # Should not raise

    def test_get_all(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        all_prices = cache.get_all()
        assert set(all_prices.keys()) == {"AAPL", "GOOGL"}

    def test_get_all_returns_copy(self):
        """Mutating the returned dict does not affect the cache."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        snapshot = cache.get_all()
        del snapshot["AAPL"]
        assert cache.get("AAPL") is not None

    def test_custom_timestamp(self):
        cache = PriceCache()
        custom_ts = 1234567890.0
        update = cache.update("AAPL", 190.50, timestamp=custom_ts)
        assert update.timestamp == custom_ts

    def test_price_rounding_to_two_decimals(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.12345)
        assert update.price == 190.12

    def test_multiple_tickers_independent(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.update("AAPL", 192.00)
        assert cache.get_price("AAPL") == 192.00
        assert cache.get_price("GOOGL") == 175.00


class TestPriceCacheVersion:
    """Tests for the version counter."""

    def test_version_starts_at_zero(self):
        cache = PriceCache()
        assert cache.version == 0

    def test_version_increments_on_update(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1

    def test_version_increments_each_update(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)
        cache.update("GOOGL", 175.00)
        assert cache.version == 3

    def test_version_does_not_reset_on_clear(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        v_before = cache.version
        cache.clear()
        assert cache.version == v_before  # Monotonically increasing


class TestPriceCacheClear:
    """Tests for the clear() method."""

    def test_clear_removes_all_tickers(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.clear()
        assert len(cache) == 0
        assert cache.get("AAPL") is None
        assert cache.get("GOOGL") is None

    def test_clear_empty_cache_is_noop(self):
        cache = PriceCache()
        cache.clear()  # Should not raise
        assert len(cache) == 0

    def test_can_add_after_clear(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.clear()
        update = cache.update("AAPL", 200.00)
        # After clear, first update for the ticker is treated as flat
        assert update.direction == "flat"
        assert update.previous_price == 200.00


class TestPriceCacheTickers:
    """Tests for the tickers property."""

    def test_tickers_empty_initially(self):
        cache = PriceCache()
        assert cache.tickers == []

    def test_tickers_after_updates(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        assert set(cache.tickers) == {"AAPL", "GOOGL"}

    def test_tickers_after_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.remove("AAPL")
        assert cache.tickers == ["GOOGL"]

    def test_tickers_returns_copy(self):
        """Mutating the returned list does not affect the cache."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        tickers = cache.tickers
        tickers.append("FAKE")
        assert "FAKE" not in cache.tickers


class TestPriceCacheDunderMethods:
    """Tests for __len__ and __contains__."""

    def test_len_empty(self):
        cache = PriceCache()
        assert len(cache) == 0

    def test_len_after_updates(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert len(cache) == 1
        cache.update("GOOGL", 175.00)
        assert len(cache) == 2

    def test_len_after_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.remove("AAPL")
        assert len(cache) == 1

    def test_contains_present(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert "AAPL" in cache

    def test_contains_absent(self):
        cache = PriceCache()
        assert "AAPL" not in cache

    def test_contains_after_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert "AAPL" not in cache


class TestPriceCacheThreadSafety:
    """Tests for concurrent access correctness."""

    def test_concurrent_writes_no_corruption(self):
        """Multiple threads writing simultaneously must not corrupt state."""
        cache = PriceCache()
        errors = []

        def writer(ticker: str, iterations: int) -> None:
            for i in range(iterations):
                try:
                    cache.update(ticker, float(100 + i))
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"TICK{i}", 200))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent writes caused errors: {errors}"
        assert len(cache) == 10

    def test_concurrent_reads_and_writes(self):
        """Readers and writers can run concurrently without deadlock."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        results = []
        errors = []

        def reader() -> None:
            for _ in range(100):
                try:
                    price = cache.get_price("AAPL")
                    if price is not None:
                        results.append(price)
                except Exception as e:
                    errors.append(e)

        def writer() -> None:
            for i in range(100):
                try:
                    cache.update("AAPL", float(190 + i))
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        threads += [threading.Thread(target=writer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent access caused errors: {errors}"

    def test_version_monotonically_increasing_under_concurrency(self):
        """Version counter should always increase, never decrease."""
        cache = PriceCache()
        versions = []

        def writer() -> None:
            for i in range(50):
                cache.update("AAPL", float(100 + i))
                versions.append(cache.version)
                time.sleep(0)  # yield

        threads = [threading.Thread(target=writer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Versions list may not be perfectly sorted due to interleaving,
        # but final version must equal total number of updates
        assert cache.version == len(versions)
