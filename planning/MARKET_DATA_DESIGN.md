# Market Data Backend — Implementation Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive (Polygon.io) API client, SSE streaming endpoint, and FastAPI lifecycle integration.

All code lives under `backend/app/market/`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Testing Strategy](#13-testing-strategy)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Configuration Summary](#15-configuration-summary)

---

## 1. Architecture Overview

The market data subsystem follows the **Strategy pattern**: two implementations (simulator and Massive API client) behind one abstract interface. All downstream code — SSE streaming, portfolio valuation, trade execution — is agnostic to which source is active.

```
MarketDataSource (ABC)
├── SimulatorDataSource   →  GBM price simulation (default, no API key needed)
└── MassiveDataSource     →  Polygon.io REST poller (when MASSIVE_API_KEY is set)
        │
        │  both write to
        ▼
   PriceCache (thread-safe, in-memory)
        │
        ├──→ SSE stream endpoint  (/api/stream/prices)  → Frontend EventSource
        ├──→ Portfolio valuation   (GET /api/portfolio)
        └──→ Trade execution       (POST /api/portfolio/trade)
```

### Data flow

1. On startup, `create_market_data_source(cache)` inspects `MASSIVE_API_KEY` and returns the appropriate source.
2. `await source.start(initial_tickers)` launches a background task.
3. The background task pushes `PriceUpdate` objects into `PriceCache` periodically:
   - Simulator: every ~500ms
   - Massive: every 15s (free tier) or 2-5s (paid tier)
4. The SSE endpoint reads from `PriceCache` every 500ms and pushes JSON events to the client.
5. Trade execution and portfolio valuation read the latest price via `cache.get_price(ticker)`.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py           # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
      │                     #   create_market_data_source, create_stream_router
      models.py             # PriceUpdate — immutable frozen dataclass
      cache.py              # PriceCache — thread-safe in-memory price store
      interface.py          # MarketDataSource — abstract base class
      seed_prices.py        # SEED_PRICES, TICKER_PARAMS, CORRELATION_GROUPS constants
      simulator.py          # GBMSimulator + SimulatorDataSource
      massive_client.py     # MassiveDataSource (Polygon.io REST poller)
      factory.py            # create_market_data_source() — selects source from env
      stream.py             # SSE endpoint factory (FastAPI router)

  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

Each file has a single responsibility. The `__init__.py` re-exports the public API so that the rest of the backend imports from `app.market` without reaching into submodules.

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type.

```python
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

### Design decisions

- **`frozen=True`**: Price updates are immutable value objects. Once created they never change, making them safe to share across async tasks without copying.
- **`slots=True`**: Minor memory optimization — many instances are created per second.
- **Computed properties** (`change`, `change_percent`, `direction`): Derived from `price` and `previous_price` so they are always consistent; no risk of a stale `direction` field.
- **`to_dict()`**: Single serialization point used by both the SSE endpoint and REST API responses.

### Example usage

```python
update = PriceUpdate(ticker="AAPL", price=191.50, previous_price=190.00)

print(update.change)          # 1.5
print(update.change_percent)  # 0.7895
print(update.direction)       # "up"
print(update.to_dict())
# {
#   "ticker": "AAPL", "price": 191.5, "previous_price": 190.0,
#   "timestamp": 1712700000.0, "change": 1.5, "change_percent": 0.7895,
#   "direction": "up"
# }
```

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The price cache is the central data hub. Data sources write to it; SSE streaming and portfolio valuation read from it. It must be thread-safe because the Massive client runs API calls via `asyncio.to_thread()` (a real OS thread), while SSE reads happen on the async event loop.

```python
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

        Automatically computes direction and change from the previous price.
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
        """Current version counter. Used by the SSE loop for change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE streaming loop polls the cache every ~500ms. Without a version counter, it would serialize and send all prices every tick even if nothing changed (e.g., Massive API updates only every 15s). The version counter lets the SSE loop skip sends when nothing is new:

```python
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:
        last_version = current_version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread safety rationale

`threading.Lock` is used instead of `asyncio.Lock` because:
- The Massive client's synchronous `get_snapshot_all()` runs in `asyncio.to_thread()`, which is a real OS thread — `asyncio.Lock` would not protect against that.
- `threading.Lock` works correctly from both OS threads and the async event loop.

### Usage example

```python
cache = PriceCache()

# Producer writes
cache.update("AAPL", 191.50)
cache.update("GOOGL", 176.20)

# Consumer reads
update = cache.get("AAPL")           # PriceUpdate or None
price = cache.get_price("AAPL")      # float or None
all_prices = cache.get_all()         # dict[str, PriceUpdate]
version = cache.version              # int

# Remove a ticker (e.g., user removes from watchlist)
cache.remove("GOOGL")
```

---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

```python
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

        Starts a background task that periodically writes to PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
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

### Why the source pushes to the cache instead of returning prices

This **push model** decouples timing:
- Simulator ticks at 500ms; Massive polls at 15s; SSE always reads from the cache at its own 500ms cadence.
- The SSE layer doesn't need to know which data source is active or what its update interval is.
- Multiple consumers (SSE, portfolio, trades) can all read from the cache simultaneously without blocking the producer.

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports. Shared by both the simulator (for initial prices and GBM parameters) and the Massive client (as fallback prices before first API response).

```python
"""Seed prices and per-ticker parameters for the market simulator."""

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
# mu: annualized drift / expected return (positive = upward bias)
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for Cholesky decomposition
# Tickers in the same group have higher intra-group pairwise correlation
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR    = 0.6   # Within tech sector
INTRA_FINANCE_CORR = 0.5   # Within finance sector
CROSS_GROUP_CORR   = 0.3   # Between sectors / unknown tickers
TSLA_CORR          = 0.3   # TSLA does its own thing (lower correlation)
```

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

The simulator uses **Geometric Brownian Motion (GBM)** to generate realistic stock price paths. GBM is the model underlying Black-Scholes option pricing — prices evolve continuously with random noise, can't go negative, and exhibit the lognormal distribution seen in real markets.

### GBM Math

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) × exp((μ − σ²/2) × dt + σ × √dt × Z)
```

Where:
- `S(t)` = current price
- `μ` (mu) = annualized drift (expected return), e.g. 0.05 = 5%
- `σ` (sigma) = annualized volatility, e.g. 0.20 = 20%
- `dt` = time step as fraction of a trading year
- `Z` = standard normal random variable drawn from N(0,1)

For 500ms updates with ~252 trading days/year and ~6.5 trading hours/day:
```
dt = 0.5 / (252 × 6.5 × 3600) ≈ 8.5e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate realistically over time.

### Correlated Moves via Cholesky Decomposition

Real stocks don't move independently — tech stocks tend to move together. We use a **Cholesky decomposition** of a correlation matrix to generate correlated random draws.

Given correlation matrix `C`, compute lower triangular `L = cholesky(C)`. For independent standard normals `Z_independent`:
```
Z_correlated = L @ Z_independent
```

Each `Z_correlated[i]` is then used in the GBM step for ticker `i`.

### GBMSimulator implementation

```python
import math
import random
from typing import Optional

import numpy as np

from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)


class GBMSimulator:
    """Generates correlated GBM price paths for multiple tickers.

    Usage:
        sim = GBMSimulator(tickers=["AAPL", "GOOGL", "TSLA"])
        prices = sim.step()   # {"AAPL": 191.23, "GOOGL": 175.88, "TSLA": 249.05}
        sim.add_ticker("MSFT")
        sim.remove_ticker("TSLA")
    """

    # dt = 0.5s / (252 trading days × 6.5 h/day × 3600 s/h)
    DT: float = 0.5 / (252 * 6.5 * 3600)

    def __init__(
        self,
        tickers: list[str],
        event_probability: float = 0.001,
    ) -> None:
        self._event_prob = event_probability
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._tickers: list[str] = []
        self._cholesky: Optional[np.ndarray] = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. No-op if already present."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. No-op if not present."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_tickers(self) -> list[str]:
        """Return the current list of tickers."""
        return list(self._tickers)

    def step(self) -> dict[str, float]:
        """Advance one time step. Returns {ticker: new_price} for all active tickers."""
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate correlated random normals via Cholesky decomposition
        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]
            dt = self.DT

            # GBM step: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma ** 2) * dt
            diffusion = sigma * math.sqrt(dt) * float(z[i])
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event (~0.1% chance per tick)
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def get_price(self, ticker: str) -> Optional[float]:
        return self._prices.get(ticker)

    # ---- Private methods ------------------------------------------------

    def _add_ticker_internal(self, ticker: str) -> None:
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50, 300))
        self._params[ticker] = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky factor of the correlation matrix."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        # Build n×n correlation matrix
        corr = np.eye(n, dtype=float)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Return the correlation coefficient between two tickers."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### SimulatorDataSource — wraps GBMSimulator in an async loop

```python
import asyncio
import logging

from .cache import PriceCache
from .interface import MarketDataSource
from .simulator_core import GBMSimulator  # same file in practice

logger = logging.getLogger(__name__)


class SimulatorDataSource(MarketDataSource):
    """Runs the GBM simulator as a background asyncio task."""

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

        # Seed the cache with initial prices before the loop starts
        # so the frontend gets data on the very first SSE poll
        initial_prices = self._sim.step()
        for ticker, price in initial_prices.items():
            self._cache.update(ticker=ticker, price=price)

        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started for %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._sim = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim and ticker not in self._sim.get_tickers():
            self._sim.add_ticker(ticker)
            # Seed cache immediately so the ticker appears on the next SSE push
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                prices = self._sim.step()  # type: ignore[union-attr]
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in simulator loop; continuing")
                await asyncio.sleep(self._interval)
```

### Behavior notes

- Prices never go negative (GBM is multiplicative — `exp()` is always positive).
- The tiny `dt` produces sub-cent moves per tick that accumulate naturally.
- With `sigma=0.50` (TSLA), a day of simulated trading produces roughly the correct intraday range.
- Random shock events fire ~0.1% of steps ≈ once per ~500s per ticker. With 10 tickers, expect an event somewhere every ~50s — enough visual drama without instability.
- When a new ticker is added mid-session, the Cholesky matrix is rebuilt in O(n²) time. With n < 50 tickers, this is negligible.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Polygon.io REST API (via the `massive` Python package) for real market data when `MASSIVE_API_KEY` is set.

### API overview

| Endpoint | Usage |
|---|---|
| `GET /v2/snapshot/.../tickers?tickers=AAPL,GOOGL,...` | Primary: batch price fetch |
| `GET /v2/snapshot/.../tickers/{ticker}` | Single-ticker detail |
| `GET /v2/aggs/ticker/{ticker}/prev` | Previous close (for day change %) |

The snapshot endpoint returns **all requested tickers in one API call** — critical for staying within the 5 req/min free-tier rate limit.

### MassiveDataSource implementation

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import SEED_PRICES

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """Polls the Polygon.io (Massive) REST API for live market prices.

    Uses the snapshot endpoint to fetch all tracked tickers in a single
    API call, staying within free-tier rate limits.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)

        # Seed cache with seed prices before first poll to avoid empty state
        for ticker in self._tickers:
            if ticker in SEED_PRICES:
                self._cache.update(ticker=ticker, price=SEED_PRICES[ticker])

        # Do one immediate poll so prices are available right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poll-loop")
        logger.info("Massive poller started for %d tickers (interval=%.0fs)",
                    len(tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            # Seed immediately with a known price if available
            if ticker in SEED_PRICES:
                self._cache.update(ticker=ticker, price=SEED_PRICES[ticker])

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # ---- Private methods ------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in Massive poll loop; will retry")

    async def _poll_once(self) -> None:
        """Fetch snapshots for all tracked tickers and update the cache."""
        if not self._tickers:
            return

        try:
            # Run the synchronous Massive client in a thread pool executor
            # to avoid blocking the event loop
            snapshots = await asyncio.to_thread(self._fetch_snapshots, list(self._tickers))
            for snap in snapshots:
                self._cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.timestamp / 1000.0,  # ms → seconds
                )
        except Exception:
            logger.exception("Failed to fetch snapshots from Massive API")

    def _fetch_snapshots(self, tickers: list[str]) -> list[Any]:
        """Synchronous call to Massive API (run in thread pool)."""
        return list(
            self._client.get_snapshot_all(
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
        )
```

### Snapshot response structure

Each element in the snapshot list has this shape (abbreviated):

```json
{
  "ticker": "AAPL",
  "last_trade": {
    "price": 191.50,
    "size": 100,
    "timestamp": 1712700000000
  },
  "day": {
    "open": 190.00,
    "high": 193.00,
    "low": 189.50,
    "close": 191.50,
    "volume": 55000000,
    "previous_close": 188.00,
    "change": 3.50,
    "change_percent": 1.86
  },
  "last_quote": {
    "bid_price": 191.49,
    "ask_price": 191.51
  }
}
```

### Poll interval guidance

| Tier | Rate limit | Recommended interval |
|---|---|---|
| Free | 5 req/min | 15s |
| Paid (starter) | Unlimited | 5s |
| Paid (premium) | Unlimited | 2s |

### Rate limit error handling

The Massive client raises `HTTPError` for 429 (rate limit exceeded). The `_poll_once` method catches all exceptions and logs them, so the poller continues running:

```python
# In MassiveDataSource._poll_once — the broad except catches 429s too.
# If the poll fails, prices simply aren't updated until the next interval.
# The cache retains the last known price, so the SSE stream continues.
```

---

## 9. Factory

**File: `backend/app/market/factory.py`**

```python
from __future__ import annotations

import os

from .cache import PriceCache
from .interface import MarketDataSource


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select and create the appropriate market data source from environment.

    If MASSIVE_API_KEY is set and non-empty, returns a MassiveDataSource.
    Otherwise, returns a SimulatorDataSource (the default).

    Imports are deferred so that the 'massive' package is only required
    when actually used.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        return SimulatorDataSource(price_cache=price_cache)
```

### Why deferred imports?

The `massive` package is an optional dependency. Without `MASSIVE_API_KEY`, users don't need it installed. Deferred imports keep the simulator path free of `massive`-related import errors.

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)
router = APIRouter()


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Register the SSE price stream endpoint on the router.

    Called once during app startup with the shared PriceCache instance.
    Returns the router for inclusion in the main FastAPI app.
    """

    @router.get("/stream/prices")
    async def price_stream() -> StreamingResponse:
        """SSE endpoint: streams live price updates to the browser.

        The client connects with EventSource('/api/stream/prices').
        Each event contains a JSON object mapping ticker → PriceUpdate dict.
        Events are sent only when the cache has been updated (version-based
        change detection).
        """
        return StreamingResponse(
            _generate_events(price_cache),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
                "Connection": "keep-alive",
            },
        )

    return router


async def _generate_events(cache: PriceCache) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price update events."""
    last_version = -1

    # Retry interval hint for browser EventSource reconnection
    yield "retry: 1000\n\n"

    while True:
        try:
            current_version = cache.version
            if current_version != last_version:
                last_version = current_version
                prices = cache.get_all()
                payload = {
                    ticker: update.to_dict()
                    for ticker, update in prices.items()
                }
                yield f"data: {json.dumps(payload)}\n\n"

            await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in SSE price stream; continuing")
            await asyncio.sleep(1.0)
```

### SSE event format

Each event sent to the browser looks like:

```
data: {"AAPL": {"ticker": "AAPL", "price": 191.50, "previous_price": 190.00,
       "timestamp": 1712700000.0, "change": 1.5, "change_percent": 0.7895,
       "direction": "up"}, "GOOGL": {...}, ...}

```

(Note the trailing blank line — required by the SSE spec to delimit events.)

### Browser-side EventSource

```typescript
const source = new EventSource('/api/stream/prices');

source.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  // Update UI for each ticker
  Object.values(prices).forEach(update => {
    flashPrice(update.ticker, update.direction);
    updateSparkline(update.ticker, update.price);
  });
};

source.onerror = () => {
  // EventSource automatically reconnects after the retry interval (1000ms)
  setConnectionStatus('reconnecting');
};
```

---

## 11. FastAPI Lifecycle Integration

The market data subsystem hooks into FastAPI's lifespan context manager so it starts and stops cleanly with the server.

```python
# backend/app/main.py

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router
from app.db import get_default_watchlist  # returns list[str] of tickers

# Shared singletons — accessible throughout the app via app.state
price_cache: PriceCache
market_source = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic for the FastAPI application."""
    global price_cache, market_source

    # --- Startup ---
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    # Load the default watchlist from the database
    initial_tickers = await get_default_watchlist()  # ["AAPL", "GOOGL", ...]
    await market_source.start(initial_tickers)

    # Store on app.state for use by route handlers
    app.state.price_cache = price_cache
    app.state.market_source = market_source

    yield  # App runs here

    # --- Shutdown ---
    await market_source.stop()


app = FastAPI(lifespan=lifespan)

# Register the SSE streaming router (needs the cache instance)
app.include_router(create_stream_router(price_cache), prefix="/api")

# Mount the Next.js static export
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### Accessing the cache in route handlers

```python
from fastapi import Request

@app.get("/api/portfolio")
async def get_portfolio(request: Request):
    cache: PriceCache = request.app.state.price_cache
    source = request.app.state.market_source
    # ...
```

---

## 12. Watchlist Coordination

When users add or remove tickers via the API, the market data source must be notified so it starts/stops producing prices for that ticker.

```python
# backend/app/routers/watchlist.py

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/watchlist")


class AddTickerRequest(BaseModel):
    ticker: str


@router.post("")
async def add_ticker(body: AddTickerRequest, request: Request):
    source = request.app.state.market_source
    cache = request.app.state.price_cache
    ticker = body.ticker.upper().strip()

    # Validate ticker format
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=400, detail="Invalid ticker symbol")

    # Add to database
    await db_add_watchlist_ticker(ticker)  # your DB layer

    # Notify market data source — it will begin producing prices
    await source.add_ticker(ticker)

    return {"ticker": ticker, "price": cache.get_price(ticker)}


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request):
    source = request.app.state.market_source
    ticker = ticker.upper().strip()

    # Remove from database
    await db_remove_watchlist_ticker(ticker)

    # Notify market data source — it will stop producing prices and
    # remove the ticker from the cache
    await source.remove_ticker(ticker)

    return {"removed": ticker}
```

---

## 13. Testing Strategy

### Unit test modules

| Module | What to test |
|---|---|
| `test_models.py` | `PriceUpdate` properties, `to_dict()`, frozen/immutable behavior |
| `test_cache.py` | Thread-safe writes, version increment, `get`/`get_all`/`remove`, initial state |
| `test_simulator.py` | GBM math correctness, `add_ticker`/`remove_ticker`, Cholesky rebuild, price positivity |
| `test_simulator_source.py` | `start`/`stop`, `add_ticker`/`remove_ticker` integration, cache seeding |
| `test_factory.py` | Returns `SimulatorDataSource` with no key, `MassiveDataSource` with key |
| `test_massive.py` | Snapshot parsing, timestamp conversion, error handling, poll loop |

### Example: PriceCache thread-safety test

```python
import threading
import pytest
from app.market.cache import PriceCache


def test_concurrent_writes_are_safe():
    cache = PriceCache()
    errors = []

    def writer(ticker: str):
        try:
            for _ in range(100):
                cache.update(ticker, 100.0 + _)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(f"T{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(cache) == 10
```

### Example: GBM math test

```python
import math
from app.market.simulator import GBMSimulator


def test_gbm_prices_stay_positive():
    sim = GBMSimulator(tickers=["AAPL", "TSLA"])
    for _ in range(1000):
        prices = sim.step()
        for ticker, price in prices.items():
            assert price > 0, f"{ticker} went non-positive: {price}"


def test_gbm_all_default_tickers():
    """Cholesky decomposition must succeed for the full 10-ticker default set."""
    tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
    sim = GBMSimulator(tickers=tickers)
    prices = sim.step()
    assert set(prices.keys()) == set(tickers)
```

### Example: Factory selection test

```python
import os
import pytest
from app.market.factory import create_market_data_source
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource
from app.market.massive_client import MassiveDataSource


def test_factory_returns_simulator_without_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    source = create_market_data_source(PriceCache())
    assert isinstance(source, SimulatorDataSource)


def test_factory_returns_massive_with_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    source = create_market_data_source(PriceCache())
    assert isinstance(source, MassiveDataSource)
```

### E2E test: SSE stream delivers prices

```python
# test/test_sse_stream.py (Playwright / httpx ASGI)
import asyncio
import httpx
import pytest
from app.main import app


@pytest.mark.asyncio
async def test_sse_stream_delivers_prices():
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

            # Collect the first data event
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    import json
                    payload = json.loads(line[5:].strip())
                    # At least one ticker should be present
                    assert len(payload) > 0
                    # Each entry should have the expected fields
                    first_entry = next(iter(payload.values()))
                    assert "ticker" in first_entry
                    assert "price" in first_entry
                    assert "direction" in first_entry
                    break
```

---

## 14. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Massive API returns 429 (rate limit) | Exception caught in `_poll_once`; last cached prices remain; loop retries after interval |
| Massive API returns 5xx | Same as above — broad exception handler, loop continues |
| Ticker not yet in cache | `cache.get("UNKNOWN")` returns `None`; callers must handle `None` |
| Trade with no current price | Trade endpoint returns 400 `{"detail": "No price available for <ticker>"}` |
| Simulator encounters zero tickers | `step()` returns `{}` immediately; no-op |
| `add_ticker` called before `start()` | Guard against `self._sim is None`; log a warning |
| `stop()` called multiple times | Idempotent — checks `task.done()` before cancelling |
| New ticker not in SEED_PRICES | Simulator assigns random price in [$50, $300]; Massive fetches real price on next poll |
| Docker restart (fresh container) | Database reloads the watchlist; `start()` re-initializes all tickers |

---

## 15. Configuration Summary

| Environment Variable | Default | Description |
|---|---|---|
| `MASSIVE_API_KEY` | (empty) | Polygon.io API key; if absent, simulator is used |
| `LLM_MOCK` | `false` | Not relevant to market data; used by the chat subsystem |

### Tunable constants (not env vars — change in code)

| Constant | Location | Default | Description |
|---|---|---|---|
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | How often the simulator steps |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | How often Massive is polled |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Probability of random shock per tick |
| `GBMSimulator.DT` | `simulator.py` | `~8.5e-8` | Time step as fraction of a trading year |
| `INTRA_TECH_CORR` | `seed_prices.py` | `0.6` | Correlation within tech sector |
| `INTRA_FINANCE_CORR` | `seed_prices.py` | `0.5` | Correlation within finance sector |
| `CROSS_GROUP_CORR` | `seed_prices.py` | `0.3` | Cross-sector / unknown-ticker correlation |

---

## Public API (`backend/app/market/__init__.py`)

```python
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceCache",
    "create_market_data_source",
    "MarketDataSource",
    "PriceUpdate",
    "create_stream_router",
]
```

### Typical startup pattern

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# Create the shared cache
cache = PriceCache()

# Create source (reads MASSIVE_API_KEY from env)
source = create_market_data_source(cache)

# Start producing prices
initial_tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]
await source.start(initial_tickers)

# Read prices anywhere
update = cache.get("AAPL")           # PriceUpdate | None
price  = cache.get_price("AAPL")     # float | None
all_p  = cache.get_all()             # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("PYPL")
await source.remove_ticker("NFLX")

# Shutdown
await source.stop()
```
