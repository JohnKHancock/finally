"""Tests for PriceUpdate dataclass."""

import time

import pytest

from app.market.models import PriceUpdate


class TestPriceUpdateCreation:
    """Tests for PriceUpdate instantiation and field access."""

    def test_basic_creation(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert update.previous_price == 190.00
        assert update.timestamp == 1234567890.0

    def test_default_timestamp_is_set(self):
        before = time.time()
        update = PriceUpdate(ticker="AAPL", price=190.00, previous_price=190.00)
        after = time.time()
        assert before <= update.timestamp <= after

    def test_zero_price_allowed(self):
        """Price of zero is valid (though unusual in practice)."""
        update = PriceUpdate(ticker="AAPL", price=0.0, previous_price=0.0, timestamp=1.0)
        assert update.price == 0.0

    def test_zero_previous_price_allowed(self):
        update = PriceUpdate(ticker="AAPL", price=100.0, previous_price=0.0, timestamp=1.0)
        assert update.previous_price == 0.0

    def test_fractional_prices(self):
        update = PriceUpdate(ticker="BTC", price=45123.456, previous_price=45000.0, timestamp=1.0)
        assert update.price == 45123.456


class TestPriceUpdateValidation:
    """Tests for PriceUpdate field validation."""

    def test_empty_ticker_raises(self):
        with pytest.raises(ValueError, match="ticker"):
            PriceUpdate(ticker="", price=100.0, previous_price=100.0, timestamp=1.0)

    def test_whitespace_ticker_raises(self):
        with pytest.raises(ValueError, match="ticker"):
            PriceUpdate(ticker="   ", price=100.0, previous_price=100.0, timestamp=1.0)

    def test_negative_price_raises(self):
        with pytest.raises(ValueError, match="price"):
            PriceUpdate(ticker="AAPL", price=-1.0, previous_price=100.0, timestamp=1.0)

    def test_negative_previous_price_raises(self):
        with pytest.raises(ValueError, match="previous_price"):
            PriceUpdate(ticker="AAPL", price=100.0, previous_price=-5.0, timestamp=1.0)


class TestPriceUpdateChange:
    """Tests for change and change_percent properties."""

    def test_change_up(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1.0)
        assert update.change == 0.50

    def test_change_down(self):
        update = PriceUpdate(ticker="AAPL", price=189.50, previous_price=190.00, timestamp=1.0)
        assert update.change == -0.50

    def test_change_zero(self):
        update = PriceUpdate(ticker="AAPL", price=190.00, previous_price=190.00, timestamp=1.0)
        assert update.change == 0.0

    def test_change_rounded_to_4_decimals(self):
        update = PriceUpdate(ticker="AAPL", price=100.00001, previous_price=100.0, timestamp=1.0)
        assert update.change == 0.0  # rounds to 0.0000 at 4 decimal places

    def test_change_percent_up(self):
        update = PriceUpdate(ticker="AAPL", price=190.00, previous_price=100.00, timestamp=1.0)
        assert update.change_percent == 90.0

    def test_change_percent_down(self):
        update = PriceUpdate(ticker="AAPL", price=100.00, previous_price=200.00, timestamp=1.0)
        assert update.change_percent == -50.0

    def test_change_percent_zero_previous(self):
        """Division by zero is handled gracefully."""
        update = PriceUpdate(ticker="AAPL", price=100.00, previous_price=0.0, timestamp=1.0)
        assert update.change_percent == 0.0

    def test_change_percent_small_move(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1.0)
        expected = round((0.50 / 190.00) * 100, 4)
        assert update.change_percent == expected

    def test_change_percent_rounded_to_4_decimals(self):
        update = PriceUpdate(ticker="AAPL", price=190.12345, previous_price=190.0, timestamp=1.0)
        result = update.change_percent
        assert isinstance(result, float)
        # Should have at most 4 decimal places
        assert round(result, 4) == result


class TestPriceUpdateDirection:
    """Tests for direction property."""

    def test_direction_up(self):
        update = PriceUpdate(ticker="AAPL", price=191.00, previous_price=190.00, timestamp=1.0)
        assert update.direction == "up"

    def test_direction_down(self):
        update = PriceUpdate(ticker="AAPL", price=189.00, previous_price=190.00, timestamp=1.0)
        assert update.direction == "down"

    def test_direction_flat(self):
        update = PriceUpdate(ticker="AAPL", price=190.00, previous_price=190.00, timestamp=1.0)
        assert update.direction == "flat"

    def test_direction_values_are_strings(self):
        for price, prev, expected in [(191.0, 190.0, "up"), (189.0, 190.0, "down"), (190.0, 190.0, "flat")]:
            update = PriceUpdate(ticker="X", price=price, previous_price=prev, timestamp=1.0)
            assert update.direction == expected


class TestPriceUpdateSerialization:
    """Tests for to_dict and from_dict."""

    def test_to_dict_keys(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        result = update.to_dict()
        assert set(result.keys()) == {"ticker", "price", "previous_price", "timestamp", "change", "change_percent", "direction"}

    def test_to_dict_values(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        result = update.to_dict()
        assert result["ticker"] == "AAPL"
        assert result["price"] == 190.50
        assert result["previous_price"] == 190.00
        assert result["timestamp"] == 1234567890.0
        assert result["change"] == 0.50
        assert result["change_percent"] == round((0.50 / 190.00) * 100, 4)
        assert result["direction"] == "up"

    def test_from_dict_roundtrip(self):
        original = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        data = original.to_dict()
        restored = PriceUpdate.from_dict(data)
        assert restored.ticker == original.ticker
        assert restored.price == original.price
        assert restored.previous_price == original.previous_price
        assert restored.timestamp == original.timestamp

    def test_from_dict_basic(self):
        data = {"ticker": "GOOGL", "price": 175.25, "previous_price": 174.00, "timestamp": 1234567890.0}
        update = PriceUpdate.from_dict(data)
        assert update.ticker == "GOOGL"
        assert update.price == 175.25
        assert update.previous_price == 174.00
        assert update.timestamp == 1234567890.0

    def test_from_dict_computed_properties(self):
        """Properties are computed correctly after from_dict."""
        data = {"ticker": "MSFT", "price": 420.0, "previous_price": 400.0, "timestamp": 1.0}
        update = PriceUpdate.from_dict(data)
        assert update.change == 20.0
        assert update.direction == "up"


class TestPriceUpdateImmutability:
    """Tests for frozen dataclass behavior."""

    def test_immutable_price(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1.0)
        with pytest.raises(AttributeError):
            update.price = 200.00

    def test_immutable_ticker(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1.0)
        with pytest.raises(AttributeError):
            update.ticker = "MSFT"

    def test_hashable(self):
        """Frozen dataclasses are hashable."""
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1.0)
        s = {update}
        assert update in s
