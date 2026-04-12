# Market Simulator Design

## Overview

The `SimulatorProvider` generates realistic, continuously-updating stock prices without any external
API dependency. It uses **Geometric Brownian Motion (GBM)** — the mathematical model underlying
the Black-Scholes options pricing formula — as its price process. The simulator runs as an
in-process `asyncio` background task and implements the same `MarketDataProvider` interface as the
Massive API client, so the rest of the system is oblivious to which source is active.

---

## Why GBM?

Geometric Brownian Motion is the standard model for equity price processes:

```
dS = μ·S·dt + σ·S·dW
```

- `S` — current price
- `μ` — drift (expected return per unit time, annualized)
- `σ` — volatility (standard deviation of returns, annualized)
- `dW` — Wiener process increment (random normal)

In discrete time (each simulator tick of interval `dt` in years):

```
S(t+dt) = S(t) · exp((μ - σ²/2)·dt + σ·√dt·Z)
```

where `Z ~ N(0, 1)`.

Key properties that make GBM appropriate here:
- **Prices stay positive** — exponential form prevents negative prices
- **Log-normal distribution** — matches observed equity return distributions
- **Configurable realism** — per-ticker drift and volatility parameters
- **Simple implementation** — one formula, one random draw per tick per ticker

---

## Simulator Parameters

### Per-Ticker Configuration

Each ticker has:
- **`seed_price`** — starting price (realistic market values)
- **`annual_drift`** — expected annualized return (e.g., 0.10 = 10% per year)
- **`annual_volatility`** — annualized volatility (e.g., 0.30 = 30% per year, typical for tech)

### Realistic Seed Prices

```python
TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"seed_price": 190.00, "annual_drift": 0.12, "annual_volatility": 0.28},
    "GOOGL": {"seed_price": 175.00, "annual_drift": 0.10, "annual_volatility": 0.30},
    "MSFT":  {"seed_price": 415.00, "annual_drift": 0.11, "annual_volatility": 0.25},
    "AMZN":  {"seed_price": 185.00, "annual_drift": 0.14, "annual_volatility": 0.35},
    "TSLA":  {"seed_price": 250.00, "annual_drift": 0.15, "annual_volatility": 0.60},
    "NVDA":  {"seed_price": 875.00, "annual_drift": 0.20, "annual_volatility": 0.55},
    "META":  {"seed_price": 505.00, "annual_drift": 0.12, "annual_volatility": 0.38},
    "JPM":   {"seed_price": 200.00, "annual_drift": 0.08, "annual_volatility": 0.22},
    "V":     {"seed_price": 275.00, "annual_drift": 0.09, "annual_volatility": 0.20},
    "NFLX":  {"seed_price": 625.00, "annual_drift": 0.13, "annual_volatility": 0.40},
    # Fallback for unknown tickers
    "_default": {"seed_price": 100.00, "annual_drift": 0.10, "annual_volatility": 0.30},
}
```

### Tick Rate

The simulator fires every **500ms** (`TICK_INTERVAL = 0.5`). Converting to annual `dt`:

```
dt = 0.5 seconds / (252 trading days × 6.5 hours × 3600 seconds)
   = 0.5 / 5,896,800
   ≈ 8.48 × 10⁻⁸  years per tick
```

At σ = 0.30, the per-tick standard deviation of log-returns is:
```
σ·√dt ≈ 0.30 × 0.000291 ≈ 0.0000873
```
This produces price moves of roughly ±0.009% per tick — realistic intraday micro-movements that
accumulate to meaningful daily swings.

---

## Correlated Moves

Real stocks in the same sector move together. The simulator implements correlation via a **factor
model**: each tick, a shared "market factor" random draw is added to each ticker's individual draw.

```python
# Each tick: generate one market shock + one idiosyncratic shock per ticker
market_shock = random.gauss(0, 1)

for ticker, state in self._states.items():
    idio_shock = random.gauss(0, 1)
    # Market beta: how much of the move comes from the market factor
    beta = state.market_beta          # typically 0.3–0.7
    Z = beta * market_shock + math.sqrt(1 - beta**2) * idio_shock
    # Apply GBM formula with combined shock Z
```

Correlation between two tickers with betas β₁ and β₂:
```
corr(ticker1, ticker2) = β₁ · β₂
```

Example: AAPL (β=0.5) and MSFT (β=0.5) → correlation ≈ 0.25. TSLA (β=0.3, more idiosyncratic)
and AAPL (β=0.5) → correlation ≈ 0.15. This creates realistic cluster behavior: tech stocks
generally move together, but with individual variation.

---

## Random Events

To add drama, the simulator occasionally injects sudden price shocks:

- **Probability**: 0.1% per tick per ticker (≈ once per 1000 ticks = ~8 minutes per ticker)
- **Magnitude**: ±2–5% instantaneous move
- **Direction**: random, but slightly biased toward the ticker's recent trend

```python
EVENT_PROBABILITY = 0.001   # per tick per ticker
EVENT_MAGNITUDE_MIN = 0.02  # 2% minimum shock
EVENT_MAGNITUDE_MAX = 0.05  # 5% maximum shock
```

---

## Full Implementation

```python
# backend/market/simulator.py

import asyncio
import math
import random
import time
import logging
from dataclasses import dataclass, field

from .interface import MarketDataProvider, PriceUpdate

logger = logging.getLogger(__name__)

TICK_INTERVAL = 0.5   # seconds between simulation steps

# Trading calendar constants for dt calculation
TRADING_DAYS_PER_YEAR = 252
TRADING_HOURS_PER_DAY = 6.5
TRADING_SECONDS_PER_YEAR = TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 3600
DT = TICK_INTERVAL / TRADING_SECONDS_PER_YEAR   # fraction of a trading year per tick

EVENT_PROBABILITY = 0.001
EVENT_MAGNITUDE_MIN = 0.02
EVENT_MAGNITUDE_MAX = 0.05

TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"seed_price": 190.00, "annual_drift": 0.12, "annual_volatility": 0.28, "market_beta": 0.50},
    "GOOGL": {"seed_price": 175.00, "annual_drift": 0.10, "annual_volatility": 0.30, "market_beta": 0.50},
    "MSFT":  {"seed_price": 415.00, "annual_drift": 0.11, "annual_volatility": 0.25, "market_beta": 0.50},
    "AMZN":  {"seed_price": 185.00, "annual_drift": 0.14, "annual_volatility": 0.35, "market_beta": 0.55},
    "TSLA":  {"seed_price": 250.00, "annual_drift": 0.15, "annual_volatility": 0.60, "market_beta": 0.30},
    "NVDA":  {"seed_price": 875.00, "annual_drift": 0.20, "annual_volatility": 0.55, "market_beta": 0.45},
    "META":  {"seed_price": 505.00, "annual_drift": 0.12, "annual_volatility": 0.38, "market_beta": 0.50},
    "JPM":   {"seed_price": 200.00, "annual_drift": 0.08, "annual_volatility": 0.22, "market_beta": 0.60},
    "V":     {"seed_price": 275.00, "annual_drift": 0.09, "annual_volatility": 0.20, "market_beta": 0.55},
    "NFLX":  {"seed_price": 625.00, "annual_drift": 0.13, "annual_volatility": 0.40, "market_beta": 0.35},
}

DEFAULT_PARAMS = {"seed_price": 100.00, "annual_drift": 0.10, "annual_volatility": 0.30, "market_beta": 0.40}


@dataclass
class TickerState:
    """Mutable state for a single simulated ticker."""
    ticker: str
    price: float                    # current price
    annual_drift: float
    annual_volatility: float
    market_beta: float              # correlation loading on market factor


class SimulatorProvider(MarketDataProvider):
    """
    In-process GBM price simulator implementing MarketDataProvider.

    Ticks every TICK_INTERVAL seconds. On each tick:
      1. Draw one market-wide shock (correlated component)
      2. For each tracked ticker, draw an idiosyncratic shock
      3. Combine shocks via factor model
      4. Apply GBM formula to update price
      5. Occasionally inject a random event (2-5% jump)
      6. Update price cache and emit callbacks
    """

    def __init__(self):
        super().__init__()
        self._states: dict[str, TickerState] = {}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # MarketDataProvider interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("SimulatorProvider started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("SimulatorProvider stopped")

    def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker not in self._states:
            params = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
            self._states[ticker] = TickerState(
                ticker=ticker,
                price=params["seed_price"],
                annual_drift=params["annual_drift"],
                annual_volatility=params["annual_volatility"],
                market_beta=params["market_beta"],
            )
            self._tickers.add(ticker)
            # Pre-populate price cache so get_price() never returns None for a known ticker
            self._price_cache[ticker] = PriceUpdate(
                ticker=ticker,
                price=params["seed_price"],
                prev_price=params["seed_price"],
                change_pct=0.0,
                timestamp=time.time(),
                direction="flat",
            )
            logger.debug(f"SimulatorProvider: added {ticker} @ {params['seed_price']}")

    def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        self._states.pop(ticker, None)
        self._tickers.discard(ticker)
        self._price_cache.pop(ticker, None)

    def get_tickers(self) -> list[str]:
        return sorted(self._tickers)

    def get_price(self, ticker: str) -> PriceUpdate | None:
        return self._price_cache.get(ticker.upper())

    def get_all_prices(self) -> dict[str, PriceUpdate]:
        return dict(self._price_cache)

    # ------------------------------------------------------------------
    # Simulation engine
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while True:
            try:
                updates = self._tick()
                if updates:
                    await self._emit(updates)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Simulator tick error: {e}", exc_info=True)
            await asyncio.sleep(TICK_INTERVAL)

    def _tick(self) -> list[PriceUpdate]:
        """Execute one simulation step. Returns list of PriceUpdates."""
        if not self._states:
            return []

        now = time.time()
        updates: list[PriceUpdate] = []

        # Shared market factor — all tickers get a correlated nudge
        market_shock = random.gauss(0, 1)

        for ticker, state in self._states.items():
            prev_price = state.price

            # Factor model: blend market shock with idiosyncratic shock
            idio_shock = random.gauss(0, 1)
            beta = state.market_beta
            Z = beta * market_shock + math.sqrt(1.0 - beta ** 2) * idio_shock

            # GBM step: S(t+dt) = S(t) * exp((mu - sigma^2/2)*dt + sigma*sqrt(dt)*Z)
            mu = state.annual_drift
            sigma = state.annual_volatility
            log_return = (mu - 0.5 * sigma ** 2) * DT + sigma * math.sqrt(DT) * Z
            new_price = state.price * math.exp(log_return)

            # Random event: occasional 2–5% jump
            if random.random() < EVENT_PROBABILITY:
                magnitude = random.uniform(EVENT_MAGNITUDE_MIN, EVENT_MAGNITUDE_MAX)
                direction = 1 if random.random() > 0.5 else -1
                new_price *= (1 + direction * magnitude)
                logger.debug(f"Simulator event: {ticker} {direction*magnitude:+.1%} jump")

            # Clamp to prevent runaway prices (optional safety net)
            seed_price = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)["seed_price"]
            new_price = max(new_price, seed_price * 0.05)   # never below 5% of seed
            new_price = min(new_price, seed_price * 20.0)   # never above 20× seed

            state.price = new_price
            change_pct = (new_price - prev_price) / prev_price * 100.0

            update = PriceUpdate(
                ticker=ticker,
                price=round(new_price, 2),
                prev_price=round(prev_price, 2),
                change_pct=round(change_pct, 4),
                timestamp=now,
                direction="up" if new_price > prev_price else ("down" if new_price < prev_price else "flat"),
            )
            self._price_cache[ticker] = update
            updates.append(update)

        return updates
```

---

## Behavioral Properties

### Price Movement per Tick

For σ = 0.30 (typical tech stock) at a 500ms tick:

| Metric | Value |
|--------|-------|
| dt (years) | 8.48 × 10⁻⁸ |
| Per-tick σ·√dt | 0.00873% |
| 1σ move per tick | ±$0.017 on a $190 stock |
| Expected daily drift at 10% annual | +0.0038% |
| Daily price range at 30% vol | ±1.9% (1σ intraday) |

### Correlation Behavior

| Pair | Beta₁ | Beta₂ | Implied Correlation |
|------|-------|-------|---------------------|
| AAPL / MSFT | 0.50 | 0.50 | 0.25 |
| AAPL / TSLA | 0.50 | 0.30 | 0.15 |
| JPM / V | 0.60 | 0.55 | 0.33 |
| NVDA / TSLA | 0.45 | 0.30 | 0.14 |

### Event Frequency

- At 0.1% probability per tick per ticker, with 10 tickers at 2 ticks/sec:
  - Expected events per minute: `10 × 2 × 60 × 0.001 = 1.2` events/minute across all tickers
  - Expected events per ticker: once every ~8.3 minutes

---

## Adding Unknown Tickers

When a ticker not in `TICKER_PARAMS` is added (e.g., via watchlist or AI chat), the simulator falls
back to `DEFAULT_PARAMS`: seed price $100, 10% drift, 30% volatility, 0.40 beta. This ensures the
simulator always works for any string, without crashing or returning `None`.

---

## Testing

Key behaviors to unit-test:

1. **GBM formula correctness** — price is always positive; log-returns are approximately normal
2. **Correlation** — over many ticks, correlation between tickers matches expected β₁·β₂
3. **Event injection** — with probability set to 1.0, every tick fires an event; magnitude in range
4. **Interface compliance** — `get_price()` never returns `None` after `add_ticker()`
5. **Price bounds** — price never drops below 5% of seed or above 20× seed
6. **Tick isolation** — removing a ticker mid-run stops updates for that ticker

```python
# backend/tests/test_simulator.py (sketch)

import asyncio
import pytest
from backend.market.simulator import SimulatorProvider


def test_prices_always_positive():
    sim = SimulatorProvider()
    sim.add_ticker("AAPL")
    for _ in range(10_000):
        updates = sim._tick()
        assert all(u.price > 0 for u in updates)


def test_price_available_immediately_after_add():
    sim = SimulatorProvider()
    sim.add_ticker("GOOGL")
    price = sim.get_price("GOOGL")
    assert price is not None
    assert price.price > 0


def test_remove_ticker_stops_updates():
    sim = SimulatorProvider()
    sim.add_ticker("AAPL")
    sim.remove_ticker("AAPL")
    updates = sim._tick()
    tickers_updated = {u.ticker for u in updates}
    assert "AAPL" not in tickers_updated


@pytest.mark.asyncio
async def test_callback_fires_on_tick():
    sim = SimulatorProvider()
    sim.add_ticker("MSFT")
    received = []
    async def cb(updates):
        received.extend(updates)
    sim.register_callback(cb)
    await sim.start()
    await asyncio.sleep(1.2)   # wait for ~2 ticks
    await sim.stop()
    assert len(received) >= 2
    assert all(u.ticker == "MSFT" for u in received)
```

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| GBM over random walk | Prices stay positive, distributional properties match real equities |
| 500ms tick interval | Fast enough for engaging price flashing, slow enough to not flood SSE clients |
| Factor model correlation | Simple single-factor approach is sufficient for visual realism |
| Pre-populate cache on add | Ensures `get_price()` never returns `None` for a known ticker; simplifies callers |
| asyncio.sleep in tick loop | Yields control between ticks; won't block the event loop |
| Clamp at 5%/2000% of seed | Prevents degenerate prices in long-running sessions without disrupting short-term realism |
| All state in `_states` dict | Simple, mutable, thread-safe within a single asyncio event loop |
