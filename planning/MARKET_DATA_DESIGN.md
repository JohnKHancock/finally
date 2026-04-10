# Market Data Backend — Design & Implementation Guide

Complete implementation guide for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration.

All code lives under `backend/app/market/`. The implementation is complete and tested — use this document as the authoritative reference when integrating market data into other backend components.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model--modelspy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Abstract Interface — `interface.py`](#5-abstract-interface--interfacepy)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters--seed_pricespy)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator--simulatorpy)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client--massive_clientpy)
9. [Factory — `factory.py`](#9-factory--factorypy)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint--streampy)
11. [Public API — `__init__.py`](#11-public-api--initpy)
12. [FastAPI Lifecycle Integration](#12-fastapi-lifecycle-integration)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Configuration Summary](#14-configuration-summary)
15. [Testing Strategy](#15-testing-strategy)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)

---

## 1. Architecture Overview

The market data subsystem follows a **strategy pattern**: two data source implementations behind one abstract interface, both feeding into a shared price cache that all downstream consumers read from.

```
MarketDataSource (ABC)          ← abstract interface
├── SimulatorDataSource         ← GBM simulator (default, no API key needed)
└── MassiveDataSource           ← Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼ writes every 500ms
   PriceCache (thread-safe, in-memory)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)  → Frontend EventSource
        ├──→ Portfolio valuation (GET /api/portfolio)
        └──→ Trade execution (POST /api/portfolio/trade)
```

### Key Design Principles

| Principle | Implementation |
|-----------|---------------|
| **Source-agnostic consumers** | Portfolio, trade, and SSE code only read from `PriceCache` — never from the data source directly |
| **Single point of truth** | `PriceCache` is the only place current prices are stored; no parallel price tracking |
| **Thread safety** | `PriceCache` uses a `threading.Lock`; the Massive client runs blocking I/O in `asyncio.to_thread` |
| **Immutable price snapshots** | `PriceUpdate` is `frozen=True` — safe to share across async tasks without copying |
| **Clean lifecycle** | `start()` → `add_ticker()`/`remove_ticker()` → `stop()` — same for both implementations |

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports public API
      models.py               # PriceUpdate dataclass
      cache.py                # PriceCache — thread-safe in-memory store
      interface.py            # MarketDataSource — abstract base class
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py            # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST poller)
      factory.py              # create_market_data_source() — env-based selection
      stream.py               # FastAPI SSE endpoint
```

Each file has a single responsibility. The `__init__.py` re-exports the public API so downstream code imports from `app.market` without reaching into submodules.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the **only data structure that leaves the market data layer**. Every downstream consumer works exclusively with this type.

```python
# backend/app/market/models.py
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Design Decisions

- **`frozen=True`** — Price updates are immutable value objects. Once created they never change, making them safe to share across async tasks without copying or defensive copies.
- **`slots=True`** — Memory optimization; we create many of these per second (2/sec × n tickers).
- **Computed properties** (`change`, `direction`, `change_percent`) — Derived from `price` and `previous_price` so they're always consistent. No risk of stale fields.
- **`to_dict()`** — Single serialization point used by both the SSE endpoint and REST API responses.

### Example Usage

```python
update = PriceUpdate(ticker="AAPL", price=191.50, previous_price=190.00)

print(update.direction)       # "up"
print(update.change)          # 1.5
print(update.change_percent)  # 0.7895
print(update.to_dict())
# {
#   "ticker": "AAPL",
#   "price": 191.5,
#   "previous_price": 190.0,
#   "timestamp": 1712700000.0,
#   "change": 1.5,
#   "change_percent": 0.7895,
#   "direction": "up"
# }
```

---

## 4. Price Cache — `cache.py`

The price cache is the **central data hub**. Data sources write to it; SSE streaming and portfolio valuation read from it. It must be thread-safe because the simulator/poller may run in a thread pool executor while SSE reads happen on the async event loop.

```python
# backend/app/market/cache.py
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        If this is the first update for a ticker, previous_price == price (direction='flat').
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Increments on every update.

        Used by the SSE endpoint for change detection — avoids sending
        redundant payloads when prices haven't changed.
        """
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Version-Based Change Detection

The `version` counter enables the SSE endpoint to avoid sending redundant events:

```python
# SSE generator pattern
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:  # New data available
        last_version = current_version
        prices = price_cache.get_all()
        yield format_sse_event(prices)
    await asyncio.sleep(0.5)
```

### Example Usage (Portfolio Valuation)

```python
# Calculate total portfolio value using live prices
from app.market import PriceCache

def calculate_portfolio_value(
    positions: list[dict],
    cash: float,
    price_cache: PriceCache,
) -> float:
    total = cash
    for position in positions:
        ticker = position["ticker"]
        qty = position["quantity"]
        current_price = price_cache.get_price(ticker)
        if current_price is not None:
            total += qty * current_price
    return total
```

---

## 5. Abstract Interface — `interface.py`

The abstract base class defines the contract that both data source implementations must fulfill.

```python
# backend/app/market/interface.py
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), no further cache writes occur.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Why an Abstract Interface?

The watchlist, portfolio, trade, and SSE endpoints only care that they can:
1. Get the current price of a ticker from the cache
2. Add/remove tickers dynamically

They never interact with the data source directly. This means:
- Swapping simulator ↔ Massive requires only a factory change
- Unit tests can mock `MarketDataSource` without simulating GBM math
- Future sources (e.g., WebSocket feeds) slot in without touching consumers

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Contains constants for the default 10-ticker watchlist.

```python
# backend/app/market/seed_prices.py

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement per tick)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing
```

### Volatility Calibration

The `sigma` values reflect real-world intraday behavior:
- `JPM` (0.18) and `V` (0.17) are stable large-cap financials — low daily ranges
- `TSLA` (0.50) has historically been one of the most volatile large-cap stocks
- `NVDA` (0.40) has high volatility plus a strong upward drift (`mu=0.08`)
- The tiny `dt` (~8.5×10⁻⁸ per 500ms tick) ensures these annualized params produce realistic per-tick moves

---

## 7. GBM Simulator — `simulator.py`

The simulator generates realistic stock price paths using **Geometric Brownian Motion (GBM)** with **Cholesky-correlated moves**.

### GBM Mathematics

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

Where:
- `S(t)` = current price
- `μ` (mu) = annualized drift (expected return), e.g., 0.05 (5%)
- `σ` (sigma) = annualized volatility, e.g., 0.22 (22%)
- `dt` = time step as a fraction of a trading year
- `Z` = standard normal random variable drawn from N(0,1)

For 500ms updates with ~252 trading days × 6.5 hours/day:
```
dt = 0.5 / (252 × 6.5 × 3600) ≈ 8.48 × 10⁻⁸
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally over simulated time.

### Why GBM?

GBM is the standard model underlying Black-Scholes option pricing:
- Prices evolve multiplicatively — they **can never go negative** (`exp()` is always positive)
- Returns follow a **lognormal distribution** matching observed market behavior
- Drift and diffusion terms map naturally to expected return and volatility

### Correlated Moves via Cholesky Decomposition

Real stocks don't move independently — tech stocks tend to rise and fall together. We generate correlated random draws using Cholesky decomposition:

```
Given correlation matrix C:
  L = cholesky(C)           # Lower-triangular matrix such that L @ L.T == C
  Z_correlated = L @ Z_independent   # Where Z_independent ~ N(0, I)
```

Result: `Z_correlated` has the desired pairwise correlations specified by `C`.

```python
# Example: correlation matrix for 3 tech stocks
import numpy as np

corr = np.array([
    [1.0, 0.6, 0.6],  # AAPL
    [0.6, 1.0, 0.6],  # GOOGL
    [0.6, 0.6, 1.0],  # MSFT
])
L = np.linalg.cholesky(corr)

# Generate correlated moves
z_independent = np.random.standard_normal(3)
z_correlated = L @ z_independent  # AAPL, GOOGL, MSFT now move together
```

### Full GBMSimulator Implementation

```python
# backend/app/market/simulator.py (GBMSimulator class)
import math
import random
import numpy as np
from .seed_prices import (
    CORRELATION_GROUPS, CROSS_GROUP_CORR, DEFAULT_PARAMS,
    INTRA_FINANCE_CORR, INTRA_TECH_CORR, SEED_PRICES,
    TICKER_PARAMS, TSLA_CORR,
)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
    """

    # 500ms expressed as a fraction of a trading year
    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability

        # Per-ticker state
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}

        # Cholesky decomposition of the correlation matrix
        self._cholesky: np.ndarray | None = None

        # Initialize all starting tickers
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws
        z_independent = np.random.standard_normal(n)

        # Apply Cholesky to get correlated draws
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec → an event roughly every 50 seconds
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        # Use seed price if known; otherwise a random price $50-$300
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild Cholesky decomposition of the ticker correlation matrix.

        Called whenever tickers are added or removed. O(n^2) but n < 50.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Determine correlation between two tickers based on sector grouping."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is in the tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR               # 0.3

        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR         # 0.6
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR      # 0.5

        return CROSS_GROUP_CORR            # 0.3
```

### SimulatorDataSource — Async Wrapper

`SimulatorDataSource` wraps `GBMSimulator` in an async task that ticks every 500ms:

```python
# backend/app/market/simulator.py (SimulatorDataSource class)
import asyncio
import logging
from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)

        # Seed the cache immediately so SSE has data on first connection
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step the simulation, write to cache, sleep."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### Simulator Behavior Notes

- Prices **never go negative** — GBM uses `exp()` which is always positive
- With `dt = 8.48e-8`, a 500ms tick produces moves of roughly `sigma × sqrt(dt)` fractional change per tick:
  - AAPL (sigma=0.22): ~0.0064% per tick → ~$0.012 on a $190 stock
  - TSLA (sigma=0.50): ~0.0145% per tick → realistic high-volatility behavior
- Random shock events occur ~0.1% of ticks. With 10 tickers at 2 ticks/sec, expect a shock somewhere ~every 50 seconds
- When tickers are added mid-session, the Cholesky matrix is rebuilt — O(n²) but n < 50 so negligible

---

## 8. Massive API Client — `massive_client.py`

The Massive client polls the Polygon.io REST API for real market data when `MASSIVE_API_KEY` is configured.

### API Overview

- **Package**: `massive` (formerly `polygon-api-client`)
- **Authentication**: API key passed to `RESTClient(api_key=...)` or read from `MASSIVE_API_KEY` env var
- **Primary endpoint**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`
- **Rate limits**: Free tier = 5 req/min (poll every 15s); paid tiers support 2-5s polling

### Full MassiveDataSource Implementation

```python
# backend/app/market/massive_client.py
from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so the cache has data right away (no blank screen)
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return
        try:
            # RESTClient is synchronous — run in a thread to avoid blocking event loop
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop retries on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread pool."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Snapshot Response Structure

Each snapshot object returned by `get_snapshot_all()` has this structure:

```python
# Fields extracted from each snapshot:
snap.ticker                        # "AAPL"
snap.last_trade.price              # 191.50 (float)
snap.last_trade.timestamp          # 1712700000000 (Unix milliseconds)
snap.day.previous_close            # 190.00 (previous session close)
snap.day.change_percent            # 0.79 (day change %)
snap.day.open                      # 189.00
snap.day.high                      # 192.00
snap.day.low                       # 188.50
snap.day.volume                    # 45000000

# We use: last_trade.price and last_trade.timestamp
# The PriceCache derives direction/change from previous cached price
```

### Rate Limit Handling

```python
# For free tier (5 req/min), configure 15s polling:
source = MassiveDataSource(
    api_key="your-key",
    price_cache=cache,
    poll_interval=15.0,  # Default — safe for free tier
)

# For paid tiers, poll more frequently:
source = MassiveDataSource(
    api_key="your-key",
    price_cache=cache,
    poll_interval=2.0,   # Up to ~30 req/min
)
```

### Key Implementation Details

- **Single API call for all tickers** — `get_snapshot_all()` fetches N tickers in one request, which is critical for staying under rate limits
- **`asyncio.to_thread()`** — The `RESTClient` is synchronous (blocking I/O). Running it in a thread pool prevents blocking the FastAPI event loop
- **Error resilience** — `_poll_once` catches all exceptions and logs them. The loop always retries on the next interval — this is essential for a long-running background service that must survive transient network failures
- **Timestamp conversion** — Massive returns Unix milliseconds; `PriceCache` expects seconds

---

## 9. Factory — `factory.py`

The factory selects which data source to use based on environment variables.

```python
# backend/app/market/factory.py
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    Selection logic:
      - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
      - Otherwise → SimulatorDataSource (GBM simulation, no external dependencies)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Environment Variable Behavior

| `MASSIVE_API_KEY` value | Result |
|------------------------|--------|
| Not set | `SimulatorDataSource` |
| Set to `""` (empty string) | `SimulatorDataSource` |
| Set to a non-empty string | `MassiveDataSource` |

This means developers can run without any API key configuration and get a fully functional simulator.

---

## 10. SSE Streaming Endpoint — `stream.py`

The SSE endpoint pushes live price updates to connected browser clients using the `EventSource` API.

```python
# backend/app/market/stream.py
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache.

    Factory pattern allows PriceCache injection without globals.
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint: GET /api/stream/prices

        Streams all tracked ticker prices every ~500ms. Event format:
            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        Includes retry directive for automatic browser reconnection.
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price events.

    Sends all prices every `interval` seconds when data has changed.
    Stops when the client disconnects.
    """
    # Tell the browser to reconnect after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:  # Data changed since last send
                last_version = current_version
                prices = price_cache.get_all()

                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### SSE Event Format

Each event is a JSON object keyed by ticker:

```
data: {"AAPL": {"ticker": "AAPL", "price": 191.50, "previous_price": 190.00, "timestamp": 1712700000.0, "change": 1.5, "change_percent": 0.7895, "direction": "up"}, "GOOGL": {...}, ...}

```

(Note: SSE events are terminated by `\n\n`)

### Frontend Connection

```typescript
// frontend/src/hooks/usePriceStream.ts
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices: Record<string, PriceUpdate> = JSON.parse(event.data);

    for (const [ticker, update] of Object.entries(prices)) {
        // Flash the price cell green/red based on direction
        flashCell(ticker, update.direction);
        // Update displayed price
        setPrice(ticker, update.price);
        // Append to sparkline data
        appendSparkline(ticker, { time: update.timestamp, price: update.price });
    }
};

// EventSource automatically reconnects after 1 second (from retry: directive)
eventSource.onerror = () => {
    setConnectionStatus('reconnecting');
};
```

### Why SSE over WebSockets?

| Factor | SSE | WebSocket |
|--------|-----|-----------|
| Direction | One-way (server→client only) | Bidirectional |
| Browser support | Native `EventSource`, all modern browsers | All modern browsers |
| Reconnection | Built-in, configurable via `retry:` | Manual implementation |
| Proxy/firewall | Works through HTTP proxies | May need special config |
| Complexity | Simple async generator | Requires connection manager |

Price streaming is one-way — the server pushes, clients receive. SSE is the right tool.

---

## 11. Public API — `__init__.py`

The module's `__init__.py` re-exports the public surface so downstream code doesn't need to know about internal submodules:

```python
# backend/app/market/__init__.py
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

### Importing from Other Backend Modules

```python
# Correct — import from the package, not from submodules
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source

# Incorrect — reaches into internals (avoid this)
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource
```

---

## 12. FastAPI Lifecycle Integration

The market data subsystem wires into FastAPI's `lifespan` context manager for clean startup and shutdown:

```python
# backend/app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router

# Default watchlist tickers (matches DB seed data)
DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]

# Module-level singletons (initialized in lifespan)
price_cache: PriceCache | None = None
market_source = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: start market data on startup, stop on shutdown."""
    global price_cache, market_source

    # Startup
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    # Load actual watchlist from database (falls back to default if DB empty)
    tickers = await load_watchlist_from_db() or DEFAULT_TICKERS
    await market_source.start(tickers)

    yield  # App runs here

    # Shutdown
    await market_source.stop()


app = FastAPI(lifespan=lifespan)

# Register the SSE streaming router (deferred until price_cache is initialized)
# This is called inside lifespan after price_cache is created:
# app.include_router(create_stream_router(price_cache))
```

### Dependency Injection Pattern

For route handlers that need access to `price_cache` or `market_source`:

```python
from fastapi import Depends

def get_price_cache() -> PriceCache:
    """FastAPI dependency that provides the shared PriceCache."""
    return price_cache  # Module-level singleton set in lifespan

def get_market_source() -> MarketDataSource:
    return market_source

# Example: trade execution endpoint reads current price
@app.post("/api/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    cache: PriceCache = Depends(get_price_cache),
    db: Connection = Depends(get_db),
):
    current_price = cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"No price data for {trade.ticker}")
    # ... execute trade at current_price ...
```

### Watchlist API Integration

When the user adds or removes tickers via the REST API, the market source must be updated:

```python
@app.post("/api/watchlist")
async def add_to_watchlist(
    body: AddTickerRequest,
    source: MarketDataSource = Depends(get_market_source),
    db: Connection = Depends(get_db),
):
    ticker = body.ticker.upper().strip()
    # Persist to database
    await db_add_ticker(db, ticker)
    # Update the live data source
    await source.add_ticker(ticker)
    return {"ticker": ticker, "status": "added"}


@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
    db: Connection = Depends(get_db),
):
    ticker = ticker.upper()
    await db_remove_ticker(db, ticker)
    # source.remove_ticker() also clears the cache entry
    await source.remove_ticker(ticker)
    return {"ticker": ticker, "status": "removed"}
```

---

## 13. Watchlist Coordination

The market source and database stay in sync via a simple pattern:

```
User action (add/remove ticker)
        │
        ▼
  REST endpoint
        │
        ├──→ Write to SQLite (watchlist table)
        └──→ await source.add_ticker() or source.remove_ticker()
                │
                ├── SimulatorDataSource: adds to GBMSimulator, seeds cache immediately
                └── MassiveDataSource: adds to _tickers list (appears on next poll)
```

On application restart, the watchlist is loaded from the database:

```python
async def load_watchlist_from_db(db: Connection) -> list[str]:
    """Load the current watchlist from SQLite at startup."""
    rows = await db.execute_fetchall(
        "SELECT ticker FROM watchlist WHERE user_id = 'default' ORDER BY added_at"
    )
    return [row["ticker"] for row in rows]

# In lifespan startup:
tickers = await load_watchlist_from_db(db) or DEFAULT_TICKERS
await market_source.start(tickers)
```

---

## 14. Configuration Summary

All configuration is via environment variables (`.env` file):

| Variable | Default | Effect |
|----------|---------|--------|
| `MASSIVE_API_KEY` | (not set) | If set: use Massive API. If absent/empty: use simulator |
| `LLM_MOCK` | `false` | Unrelated to market data — controls LLM mock mode for tests |

### Simulator Configuration (Code Constants)

| Parameter | Value | Effect |
|-----------|-------|--------|
| `update_interval` | `0.5s` | How often the simulator ticks |
| `event_probability` | `0.001` | ~0.1% chance of shock event per tick per ticker |
| `DEFAULT_DT` | `~8.48e-8` | GBM time step (500ms / trading seconds per year) |

### Massive API Configuration

| Parameter | Default | Notes |
|-----------|---------|-------|
| `poll_interval` | `15.0s` | Safe for free tier (5 req/min). Reduce for paid tiers |

To override `poll_interval` or `update_interval`, modify the `create_market_data_source()` factory or the `SimulatorDataSource`/`MassiveDataSource` constructors directly.

---

## 15. Testing Strategy

### Unit Tests (in `backend/tests/market/`)

**`test_models.py`** — `PriceUpdate` dataclass:
```python
def test_direction_up():
    update = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
    assert update.direction == "up"
    assert update.change == 1.0
    assert update.change_percent == pytest.approx(0.5263, rel=1e-3)

def test_direction_flat():
    update = PriceUpdate(ticker="AAPL", price=190.0, previous_price=190.0)
    assert update.direction == "flat"
    assert update.change == 0.0

def test_to_dict_has_all_fields():
    update = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0, timestamp=1000.0)
    d = update.to_dict()
    assert set(d.keys()) == {"ticker", "price", "previous_price", "timestamp",
                              "change", "change_percent", "direction"}
```

**`test_cache.py`** — `PriceCache` thread safety and behavior:
```python
def test_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.0)
    assert update.direction == "flat"
    assert update.previous_price == 190.0

def test_version_increments_on_update():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.0)
    cache.update("GOOGL", 175.0)
    assert cache.version == v0 + 2

def test_thread_safety():
    import threading
    cache = PriceCache()
    errors = []

    def writer():
        try:
            for i in range(100):
                cache.update("AAPL", 190.0 + i)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert "AAPL" in cache
```

**`test_simulator.py`** — GBM math and behavior:
```python
def test_prices_never_negative():
    sim = GBMSimulator(tickers=["AAPL", "TSLA"])
    for _ in range(1000):
        prices = sim.step()
        for ticker, price in prices.items():
            assert price > 0, f"{ticker} went negative: {price}"

def test_correlated_moves_shape():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL", "MSFT"])
    assert sim._cholesky is not None
    assert sim._cholesky.shape == (3, 3)

def test_add_remove_ticker():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("TSLA")
    assert "TSLA" in sim.get_tickers()
    sim.remove_ticker("TSLA")
    assert "TSLA" not in sim.get_tickers()
```

**`test_factory.py`** — Environment-based selection:
```python
def test_no_api_key_returns_simulator(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_api_key_returns_massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)

def test_empty_api_key_returns_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

### Running Tests

```bash
cd backend
uv sync --extra dev        # Install test dependencies
uv run --extra dev pytest -v               # All tests
uv run --extra dev pytest --cov=app        # With coverage report
uv run --extra dev pytest tests/market/    # Just market data tests
uv run --extra dev ruff check app/ tests/  # Lint check
```

### Live Demo

A Rich terminal dashboard is available for visual testing:

```bash
cd backend
uv run market_data_demo.py
```

Displays all 10 tickers with live-updating prices, sparklines, color-coded direction arrows, and an event log for shock events. Runs for 60 seconds (or until Ctrl+C).

---

## 16. Error Handling & Edge Cases

### Empty Watchlist

Both implementations handle an empty ticker list gracefully:

```python
# GBMSimulator.step() — returns empty dict when no tickers
def step(self) -> dict[str, float]:
    n = len(self._tickers)
    if n == 0:
        return {}
    # ...

# MassiveDataSource._poll_once() — no-op when no tickers
async def _poll_once(self) -> None:
    if not self._tickers or not self._client:
        return
    # ...
```

### Unknown Ticker Added to Simulator

```python
# Tickers not in SEED_PRICES get a random starting price $50-$300
# Tickers not in TICKER_PARAMS get default GBM params (sigma=0.25, mu=0.05)
sim.add_ticker("PYPL")  # Gets random seed price, default params — works fine
```

### Massive API Failures

`_poll_once` logs errors and returns without raising — the poll loop continues on the next interval:

```python
# Common failure scenarios handled:
# 401 Unauthorized — bad API key (logged as error, loop continues)
# 429 Too Many Requests — rate limit hit (logged, loop continues with same interval)
# 503 Service Unavailable — transient API error (logged, retries on next poll)
# Network timeout — caught by general Exception handler

# Malformed individual snapshots are skipped without failing the whole batch:
try:
    price = snap.last_trade.price
    self._cache.update(ticker=snap.ticker, price=price, ...)
except (AttributeError, TypeError) as e:
    logger.warning("Skipping snapshot for %s: %s", snap.ticker, e)
```

### Ticker Not in Cache

```python
# Callers must handle None from cache reads
price = cache.get_price("AAPL")
if price is None:
    # Ticker not yet tracked or not yet polled
    raise HTTPException(400, "Price not available for AAPL")

# Or use a fallback:
update = cache.get("AAPL")
price = update.price if update else DEFAULT_PRICE
```

### SSE Client Disconnection

The SSE generator detects disconnection via `request.is_disconnected()` and stops the generator cleanly. FastAPI/Starlette cleans up the `StreamingResponse` automatically.

```python
while True:
    if await request.is_disconnected():
        break  # Clean exit — no resource leak
    # ... generate events ...
```

### Stop Called Multiple Times

Both implementations guard against double-stop:

```python
async def stop(self) -> None:
    if self._task and not self._task.done():  # Guard: task must exist and be running
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
    self._task = None  # Idempotent: calling stop() again is a no-op
```
