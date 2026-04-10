"""Tests for GBMSimulator."""

import math

import numpy as np

from app.market.seed_prices import SEED_PRICES, TICKER_PARAMS
from app.market.simulator import GBMSimulator


DEFAULT_TICKERS = list(SEED_PRICES.keys())  # The 10 default tickers


class TestGBMSimulatorInitialization:
    """Tests for GBMSimulator construction."""

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_all_default_tickers_initialized(self):
        """All 10 default tickers can be initialized at once."""
        sim = GBMSimulator(tickers=DEFAULT_TICKERS)
        for ticker in DEFAULT_TICKERS:
            price = sim.get_price(ticker)
            assert price is not None, f"{ticker} missing initial price"
            assert price > 0, f"{ticker} initial price is non-positive: {price}"

    def test_all_default_tickers_match_seeds(self):
        sim = GBMSimulator(tickers=DEFAULT_TICKERS)
        for ticker in DEFAULT_TICKERS:
            assert sim.get_price(ticker) == SEED_PRICES[ticker]

    def test_unknown_ticker_gets_random_seed_price(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert price is not None
        assert 50.0 <= price <= 300.0

    def test_empty_tickers_list(self):
        sim = GBMSimulator(tickers=[])
        assert sim.get_tickers() == []

    def test_get_tickers_returns_copy(self):
        sim = GBMSimulator(tickers=["AAPL"])
        tickers = sim.get_tickers()
        tickers.append("FAKE")
        assert "FAKE" not in sim.get_tickers()


class TestGBMSimulatorStep:
    """Tests for the step() hot path."""

    def test_prices_are_positive(self):
        """GBM produces positive prices (exp is always > 0)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_prices_rounded_to_two_decimals(self):
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(100):
            result = sim.step()
            price_str = str(result["AAPL"])
            if "." in price_str:
                decimal_part = price_str.split(".")[1]
                assert len(decimal_part) <= 2, f"Price {result['AAPL']} has too many decimals"

    def test_empty_step_returns_empty_dict(self):
        sim = GBMSimulator(tickers=[])
        result = sim.step()
        assert result == {}

    def test_prices_change_over_time(self):
        sim = GBMSimulator(tickers=["AAPL"])
        initial_price = sim.get_price("AAPL")
        for _ in range(1000):
            sim.step()
        final_price = sim.get_price("AAPL")
        assert final_price != initial_price

    def test_all_default_tickers_step(self):
        """A single step with all 10 tickers returns all 10 prices."""
        sim = GBMSimulator(tickers=DEFAULT_TICKERS)
        result = sim.step()
        assert set(result.keys()) == set(DEFAULT_TICKERS)
        for ticker, price in result.items():
            assert price > 0, f"{ticker} stepped to non-positive price: {price}"

    def test_get_prices_snapshot(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.step()
        prices = sim.get_prices()
        assert set(prices.keys()) == {"AAPL", "GOOGL"}
        for price in prices.values():
            assert price > 0

    def test_get_prices_returns_copy(self):
        sim = GBMSimulator(tickers=["AAPL"])
        prices = sim.get_prices()
        prices["AAPL"] = 0.0
        assert sim.get_price("AAPL") != 0.0


class TestGBMSimulatorDynamicTickers:
    """Tests for adding and removing tickers dynamically."""

    def test_add_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        result = sim.step()
        assert "TSLA" in result

    def test_add_ticker_seeds_price(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        assert sim.get_price("TSLA") == SEED_PRICES["TSLA"]

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim.get_tickers()) == 1

    def test_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result

    def test_remove_nonexistent_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.remove_ticker("NOPE")  # Should not raise
        assert sim.get_tickers() == ["AAPL"]

    def test_remove_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("AAPL")
        sim.remove_ticker("GOOGL")
        assert sim.get_tickers() == []
        assert sim.step() == {}

    def test_get_price_returns_none_for_unknown(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("UNKNOWN") is None


class TestGBMSimulatorCorrelation:
    """Tests for the Cholesky correlation matrix."""

    def test_cholesky_none_with_zero_tickers(self):
        sim = GBMSimulator(tickers=[])
        assert sim._cholesky is None

    def test_cholesky_none_with_one_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None

    def test_cholesky_exists_with_two_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sim._cholesky is not None

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None

    def test_cholesky_removed_when_back_to_one(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sim._cholesky is not None
        sim.remove_ticker("GOOGL")
        assert sim._cholesky is None

    def test_cholesky_shape_matches_ticker_count(self):
        for n in [2, 5, 10]:
            tickers = DEFAULT_TICKERS[:n]
            sim = GBMSimulator(tickers=tickers)
            assert sim._cholesky.shape == (n, n), f"Expected ({n},{n}), got {sim._cholesky.shape}"

    def test_cholesky_valid_for_all_10_tickers(self):
        """Correlation matrix for all 10 default tickers must be positive definite."""
        sim = GBMSimulator(tickers=DEFAULT_TICKERS)
        L = sim._cholesky
        assert L is not None
        # L @ L.T should reconstruct the correlation matrix
        corr = L @ L.T
        # Diagonal should be 1.0 (self-correlation)
        for i in range(len(DEFAULT_TICKERS)):
            assert abs(corr[i, i] - 1.0) < 1e-10, f"Diagonal [{i},{i}] = {corr[i,i]}"
        # All off-diagonal entries should be between 0 and 1
        n = len(DEFAULT_TICKERS)
        for i in range(n):
            for j in range(n):
                if i != j:
                    assert 0 <= corr[i, j] <= 1.0, f"corr[{i},{j}] = {corr[i,j]} out of range"

    def test_pairwise_correlation_tech_stocks(self):
        corr = GBMSimulator._pairwise_correlation("AAPL", "GOOGL")
        assert corr == 0.6

    def test_pairwise_correlation_finance_stocks(self):
        corr = GBMSimulator._pairwise_correlation("JPM", "V")
        assert corr == 0.5

    def test_pairwise_correlation_tsla_with_tech(self):
        corr = GBMSimulator._pairwise_correlation("TSLA", "AAPL")
        assert corr == 0.3

    def test_pairwise_correlation_tsla_with_finance(self):
        corr = GBMSimulator._pairwise_correlation("TSLA", "JPM")
        assert corr == 0.3

    def test_pairwise_correlation_cross_sector(self):
        corr = GBMSimulator._pairwise_correlation("AAPL", "JPM")
        assert corr == 0.3

    def test_pairwise_correlation_unknown_tickers(self):
        corr = GBMSimulator._pairwise_correlation("ZZZZ", "XXXX")
        assert corr == 0.3

    def test_pairwise_correlation_symmetric(self):
        """Correlation is symmetric: corr(A, B) == corr(B, A)."""
        tickers = DEFAULT_TICKERS
        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                assert GBMSimulator._pairwise_correlation(tickers[i], tickers[j]) == \
                       GBMSimulator._pairwise_correlation(tickers[j], tickers[i])


class TestGBMSimulatorParameters:
    """Tests for GBM parameter tuning."""

    def test_default_dt_is_reasonable(self):
        assert 0 < GBMSimulator.DEFAULT_DT < 0.0001

    def test_default_dt_calculation(self):
        expected = 0.5 / (252 * 6.5 * 3600)
        assert abs(GBMSimulator.DEFAULT_DT - expected) < 1e-15

    def test_high_volatility_ticker_more_variable(self):
        """TSLA (sigma=0.5) should have more price variance than V (sigma=0.17)."""
        n_steps = 10_000
        tsla_prices = []
        v_prices = []

        tsla_sim = GBMSimulator(tickers=["TSLA"])
        v_sim = GBMSimulator(tickers=["V"])

        for _ in range(n_steps):
            tsla_prices.append(tsla_sim.step()["TSLA"])
            v_prices.append(v_sim.step()["V"])

        tsla_std = float(np.std(tsla_prices))
        v_std = float(np.std(v_prices))
        # TSLA has 3x higher sigma; it should have higher variance in practice
        assert tsla_std > v_std, f"TSLA std {tsla_std:.4f} should > V std {v_std:.4f}"

    def test_shock_events_bounded(self):
        """Even with 100% shock probability, prices stay within 5% per tick."""
        sim = GBMSimulator(tickers=["AAPL"], event_probability=1.0)
        prev_price = sim.get_price("AAPL")
        for _ in range(100):
            result = sim.step()
            current_price = result["AAPL"]
            # Maximum shock is 5%, plus tiny GBM drift
            ratio = current_price / prev_price if prev_price else 1.0
            assert 0.9 <= ratio <= 1.12, f"Price jump ratio {ratio} out of expected range"
            prev_price = current_price

    def test_gbm_formula_uses_exp(self):
        """GBM prices follow log-normal distribution (always positive via exp)."""
        sim = GBMSimulator(tickers=["AAPL"], event_probability=0.0)
        prices = [sim.step()["AAPL"] for _ in range(10_000)]
        assert all(p > 0 for p in prices)
        # Log of log-normal should be approximately normal
        log_prices = [math.log(p) for p in prices]
        mean_log = sum(log_prices) / len(log_prices)
        assert abs(mean_log - math.log(SEED_PRICES["AAPL"])) < 5  # Reasonable drift
