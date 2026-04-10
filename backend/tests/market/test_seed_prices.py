"""Tests for seed prices and GBM parameters."""


from app.market.seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


class TestSeedPrices:
    """Tests for the SEED_PRICES dictionary."""

    def test_all_default_tickers_have_seed_prices(self):
        for ticker in DEFAULT_TICKERS:
            assert ticker in SEED_PRICES, f"{ticker} missing from SEED_PRICES"

    def test_all_seed_prices_are_positive(self):
        for ticker, price in SEED_PRICES.items():
            assert price > 0, f"{ticker} seed price {price} is not positive"

    def test_seed_prices_are_realistic(self):
        """Prices should be in a plausible range for US equities."""
        for ticker, price in SEED_PRICES.items():
            assert 1.0 <= price <= 10_000.0, f"{ticker} seed price {price} is out of realistic range"

    def test_exactly_10_default_tickers(self):
        assert len(SEED_PRICES) == 10

    def test_specific_seed_prices(self):
        """Spot-check a few known seed prices for regression."""
        assert SEED_PRICES["AAPL"] == 190.00
        assert SEED_PRICES["GOOGL"] == 175.00
        assert SEED_PRICES["MSFT"] == 420.00
        assert SEED_PRICES["NVDA"] == 800.00

    def test_seed_prices_are_floats(self):
        for ticker, price in SEED_PRICES.items():
            assert isinstance(price, float), f"{ticker} seed price {price!r} is not a float"


class TestTickerParams:
    """Tests for the TICKER_PARAMS dictionary."""

    def test_all_default_tickers_have_params(self):
        for ticker in DEFAULT_TICKERS:
            assert ticker in TICKER_PARAMS, f"{ticker} missing from TICKER_PARAMS"

    def test_params_have_required_keys(self):
        for ticker, params in TICKER_PARAMS.items():
            assert "sigma" in params, f"{ticker} params missing 'sigma'"
            assert "mu" in params, f"{ticker} params missing 'mu'"

    def test_volatility_positive(self):
        for ticker, params in TICKER_PARAMS.items():
            assert params["sigma"] > 0, f"{ticker} sigma {params['sigma']} is not positive"

    def test_volatility_reasonable_range(self):
        """Annualized volatility should be between 5% and 200%."""
        for ticker, params in TICKER_PARAMS.items():
            assert 0.05 <= params["sigma"] <= 2.0, \
                f"{ticker} sigma {params['sigma']} is outside [0.05, 2.0]"

    def test_drift_reasonable_range(self):
        """Annualized drift should be between -50% and +100%."""
        for ticker, params in TICKER_PARAMS.items():
            assert -0.5 <= params["mu"] <= 1.0, \
                f"{ticker} mu {params['mu']} is outside [-0.5, 1.0]"

    def test_tsla_high_volatility(self):
        """TSLA should be among the most volatile tickers."""
        tsla_sigma = TICKER_PARAMS["TSLA"]["sigma"]
        v_sigma = TICKER_PARAMS["V"]["sigma"]
        assert tsla_sigma > v_sigma, "TSLA should be more volatile than V"

    def test_finance_stocks_lower_volatility(self):
        """Finance stocks (JPM, V) should have lower volatility than TSLA."""
        for ticker in ["JPM", "V"]:
            assert TICKER_PARAMS[ticker]["sigma"] < TICKER_PARAMS["TSLA"]["sigma"]

    def test_nvda_strong_drift(self):
        """NVDA is configured with higher expected return."""
        assert TICKER_PARAMS["NVDA"]["mu"] >= TICKER_PARAMS["AAPL"]["mu"]


class TestDefaultParams:
    """Tests for DEFAULT_PARAMS (used for dynamically added tickers)."""

    def test_default_params_has_sigma(self):
        assert "sigma" in DEFAULT_PARAMS

    def test_default_params_has_mu(self):
        assert "mu" in DEFAULT_PARAMS

    def test_default_sigma_positive(self):
        assert DEFAULT_PARAMS["sigma"] > 0

    def test_default_params_reasonable(self):
        assert 0.05 <= DEFAULT_PARAMS["sigma"] <= 2.0
        assert -0.5 <= DEFAULT_PARAMS["mu"] <= 1.0


class TestCorrelationGroups:
    """Tests for the sector correlation structure."""

    def test_correlation_groups_has_tech(self):
        assert "tech" in CORRELATION_GROUPS

    def test_correlation_groups_has_finance(self):
        assert "finance" in CORRELATION_GROUPS

    def test_tech_group_contains_expected_tickers(self):
        tech = CORRELATION_GROUPS["tech"]
        for ticker in ["AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"]:
            assert ticker in tech, f"{ticker} not in tech group"

    def test_finance_group_contains_expected_tickers(self):
        finance = CORRELATION_GROUPS["finance"]
        for ticker in ["JPM", "V"]:
            assert ticker in finance, f"{ticker} not in finance group"

    def test_groups_are_disjoint(self):
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]
        overlap = tech & finance
        assert overlap == set(), f"Overlap between tech and finance: {overlap}"


class TestCorrelationCoefficients:
    """Tests for the correlation constant values."""

    def test_intra_tech_corr_value(self):
        assert INTRA_TECH_CORR == 0.6

    def test_intra_finance_corr_value(self):
        assert INTRA_FINANCE_CORR == 0.5

    def test_cross_group_corr_value(self):
        assert CROSS_GROUP_CORR == 0.3

    def test_tsla_corr_value(self):
        assert TSLA_CORR == 0.3

    def test_correlations_in_valid_range(self):
        """All correlations must be strictly between 0 and 1 for Cholesky to succeed."""
        for name, corr in [
            ("INTRA_TECH_CORR", INTRA_TECH_CORR),
            ("INTRA_FINANCE_CORR", INTRA_FINANCE_CORR),
            ("CROSS_GROUP_CORR", CROSS_GROUP_CORR),
            ("TSLA_CORR", TSLA_CORR),
        ]:
            assert 0 < corr < 1, f"{name} = {corr} must be in (0, 1)"

    def test_intra_tech_higher_than_cross_sector(self):
        assert INTRA_TECH_CORR > CROSS_GROUP_CORR

    def test_intra_finance_higher_than_cross_sector(self):
        assert INTRA_FINANCE_CORR > CROSS_GROUP_CORR
