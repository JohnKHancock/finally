# Market Data Interface Design

## Overview

The market data layer uses a single abstract interface (`MarketDataProvider`) with two concrete
implementations: `MassiveProvider` (real data via Massive API) and `SimulatorProvider` (in-process
GBM simulator). The backend selects which to instantiate at startup based on the `MASSIVE_API_KEY`
environment variable. All downstream code — SSE streaming, the price cache, API routes — is
written against the interface only.

---

## Selection Logic

```python
# backend/market/factory.py

import os
from .interface import MarketDataProvider
from .massive import MassiveProvider
from .simulator import SimulatorProvider

def create_provider() -> MarketDataProvider:
    """Instantiate the correct provider based on environment."""
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if api_key:
        poll_interval = float(os.getenv("MASSIVE_POLL_INTERVAL_SECONDS", "15"))
        return MassiveProvider(api_key=api_key, poll_interval=poll_interval)
    return SimulatorProvider()
```

This is called once at application startup and the returned instance is stored as a module-level
singleton, injected into FastAPI via `app.state` or as a dependency.

---

## Abstract Interface

```python
# backend/market/interface.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import asyncio


@dataclass
class PriceUpdate:
    """A single price update event for one ticker."""
    ticker: str
    price: float
    prev_price: float
    change_pct: float          # percentage change from prev_price
    timestamp: float           # Unix epoch seconds (float)
    direction: str             # "up", "down", or "flat"


@dataclass
class TickerInfo:
    """Metadata for a ticker (used when adding to watchlist)."""
    ticker: str
    name: str = ""
    valid: bool = True


PriceCallback = Callable[[list[PriceUpdate]], Awaitable[None]]


class MarketDataProvider(ABC):
    """
    Abstract base class for all market data sources.

    Lifecycle:
      1. start()      — called once at app startup; begins background polling/simulation
      2. (running)    — calls registered callbacks on each update cycle
      3. stop()       — called at app shutdown; cancels background tasks cleanly

    Watchlist:
      The provider maintains an internal set of tracked tickers. Only tickers in this
      set are included in update callbacks. The set is mutable at runtime via
      add_ticker / remove_ticker.

    Price cache:
      The provider maintains a cache of the latest PriceUpdate for each ticker.
      get_price() returns from this cache synchronously (no I/O).
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the background data task. Must be idempotent."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background data task and clean up resources."""
        ...

    @abstractmethod
    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the tracked set. Takes effect on next poll/tick."""
        ...

    @abstractmethod
    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the tracked set."""
        ...

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of tracked tickers."""
        ...

    @abstractmethod
    def get_price(self, ticker: str) -> PriceUpdate | None:
        """
        Return the latest cached PriceUpdate for a ticker, or None if not yet available.
        Synchronous — reads from in-memory cache, no I/O.
        """
        ...

    @abstractmethod
    def get_all_prices(self) -> dict[str, PriceUpdate]:
        """Return the full cache: {ticker: PriceUpdate} for all tracked tickers."""
        ...

    def register_callback(self, callback: PriceCallback) -> None:
        """
        Register an async callback to be invoked after each update cycle.
        Callbacks receive the list of PriceUpdates that changed this cycle.
        Multiple callbacks can be registered (e.g. one for SSE, one for snapshots).
        """
        self._callbacks.append(callback)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __init__(self):
        self._callbacks: list[PriceCallback] = []
        self._price_cache: dict[str, PriceUpdate] = {}
        self._tickers: set[str] = set()

    async def _emit(self, updates: list[PriceUpdate]) -> None:
        """Call all registered callbacks with the update batch."""
        for cb in self._callbacks:
            await cb(updates)
```

---

## MassiveProvider Implementation

```python
# backend/market/massive.py

import asyncio
import time
import logging
from massive import RESTClient
from .interface import MarketDataProvider, PriceUpdate

logger = logging.getLogger(__name__)


class MassiveProvider(MarketDataProvider):
    """
    Polls the Massive API snapshot endpoint on a configurable interval.
    A single request fetches all tracked tickers at once.
    """

    def __init__(self, api_key: str, poll_interval: float = 15.0):
        super().__init__()
        self._api_key = api_key
        self._poll_interval = poll_interval
        self._client = RESTClient(api_key=api_key)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"MassiveProvider started (interval={self._poll_interval}s)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def add_ticker(self, ticker: str) -> None:
        self._tickers.add(ticker.upper())

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker.upper())

    def get_tickers(self) -> list[str]:
        return sorted(self._tickers)

    def get_price(self, ticker: str) -> PriceUpdate | None:
        return self._price_cache.get(ticker.upper())

    def get_all_prices(self) -> dict[str, PriceUpdate]:
        return dict(self._price_cache)

    async def _poll_loop(self) -> None:
        while True:
            try:
                if self._tickers:
                    updates = await asyncio.to_thread(self._fetch_prices)
                    if updates:
                        await self._emit(updates)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"MassiveProvider poll error: {e}")
            await asyncio.sleep(self._poll_interval)

    def _fetch_prices(self) -> list[PriceUpdate]:
        """Synchronous fetch — runs in a thread pool via asyncio.to_thread."""
        tickers = list(self._tickers)
        if not tickers:
            return []

        try:
            # Single request for all tracked tickers
            snapshot = self._client.get_snapshot_all_tickers(
                locale="us",
                market_type="stocks",
                tickers=",".join(tickers),
            )
        except Exception as e:
            logger.error(f"Massive API request failed: {e}")
            return []

        updates: list[PriceUpdate] = []
        now = time.time()

        for t in (snapshot.tickers or []):
            ticker = t.ticker
            if not ticker:
                continue

            # Best-effort price extraction: lastTrade → min bar → day bar
            price = None
            if t.last_trade and t.last_trade.price:
                price = float(t.last_trade.price)
            elif t.min and t.min.c:
                price = float(t.min.c)
            elif t.day and t.day.c:
                price = float(t.day.c)

            if price is None:
                logger.warning(f"No price data for {ticker}")
                continue

            prev = self._price_cache.get(ticker)
            prev_price = prev.price if prev else price
            change_pct = ((price - prev_price) / prev_price * 100) if prev_price else 0.0

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                prev_price=prev_price,
                change_pct=change_pct,
                timestamp=now,
                direction="up" if price > prev_price else ("down" if price < prev_price else "flat"),
            )
            self._price_cache[ticker] = update
            updates.append(update)

        return updates
```

---

## SimulatorProvider (stub — see MARKET_SIMULATOR.md for full detail)

```python
# backend/market/simulator.py  (abbreviated — see MARKET_SIMULATOR.md)

import asyncio
from .interface import MarketDataProvider, PriceUpdate

class SimulatorProvider(MarketDataProvider):
    """GBM-based price simulator. Full implementation in MARKET_SIMULATOR.md."""

    def __init__(self):
        super().__init__()
        # See MARKET_SIMULATOR.md for full __init__

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def add_ticker(self, ticker: str) -> None: ...
    def remove_ticker(self, ticker: str) -> None: ...
    def get_tickers(self) -> list[str]: ...
    def get_price(self, ticker: str) -> PriceUpdate | None: ...
    def get_all_prices(self) -> dict[str, PriceUpdate]: ...
```

---

## FastAPI Integration

### App Startup / Shutdown

```python
# backend/main.py

from contextlib import asynccontextmanager
from fastapi import FastAPI
from .market.factory import create_provider
from .db.init import init_db, get_default_tickers

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB and seed defaults
    await init_db()

    # Create and start market data provider
    provider = create_provider()
    tickers = await get_default_tickers()   # reads watchlist table
    for ticker in tickers:
        provider.add_ticker(ticker)

    # Wire SSE callback
    from .routes.stream import on_price_update
    provider.register_callback(on_price_update)

    await provider.start()
    app.state.market = provider

    yield   # app is running

    await provider.stop()


app = FastAPI(lifespan=lifespan)
```

### Dependency Injection

```python
# backend/deps.py

from fastapi import Request
from .market.interface import MarketDataProvider

def get_market(request: Request) -> MarketDataProvider:
    return request.app.state.market
```

```python
# backend/routes/watchlist.py (example)

from fastapi import APIRouter, Depends
from ..deps import get_market
from ..market.interface import MarketDataProvider

router = APIRouter()

@router.post("/api/watchlist")
async def add_ticker(body: dict, market: MarketDataProvider = Depends(get_market)):
    ticker = body["ticker"].upper()
    market.add_ticker(ticker)
    # ... also persist to DB
    return {"ticker": ticker}
```

### SSE Stream

```python
# backend/routes/stream.py

import asyncio
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from ..market.interface import PriceUpdate

router = APIRouter()

# Queue per connected SSE client
_queues: list[asyncio.Queue] = []

async def on_price_update(updates: list[PriceUpdate]) -> None:
    """Callback registered with the provider — fans out to all SSE queues."""
    payload = [
        {
            "ticker": u.ticker,
            "price": u.price,
            "prev_price": u.prev_price,
            "change_pct": u.change_pct,
            "direction": u.direction,
            "timestamp": u.timestamp,
        }
        for u in updates
    ]
    data = json.dumps(payload)
    for q in _queues:
        await q.put(data)


@router.get("/api/stream/prices")
async def stream_prices():
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _queues.append(queue)

    async def event_generator():
        try:
            while True:
                data = await queue.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _queues.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

---

## Price Cache Contract

The price cache (`get_all_prices()` / `get_price()`) provides the single source of truth for
current prices used by:

- **SSE stream** — via callback (push model)
- **`GET /api/watchlist`** — attaches latest price to each watchlist entry
- **`GET /api/portfolio`** — computes unrealized P&L using current price
- **Trade execution** — fills market orders at `get_price(ticker).price`
- **Portfolio snapshots** — background task sums positions × current prices every 30s

Rules:
1. Always read from cache (sync, no I/O) — never make a live API call in a request handler
2. `get_price()` returns `None` if the ticker has not yet received its first update — callers must handle this
3. The cache is updated by the background task only — no other code writes to it

---

## File Layout

```
backend/
└── market/
    ├── __init__.py
    ├── factory.py       # create_provider() — reads env var, returns provider instance
    ├── interface.py     # MarketDataProvider ABC + PriceUpdate dataclass
    ├── massive.py       # MassiveProvider — REST polling implementation
    └── simulator.py     # SimulatorProvider — GBM simulation implementation
```

---

## Environment Variables Summary

| Variable                         | Default | Description |
|----------------------------------|---------|-------------|
| `MASSIVE_API_KEY`                | (empty) | If set, uses Massive API; otherwise uses simulator |
| `MASSIVE_POLL_INTERVAL_SECONDS`  | `15`    | Poll interval in seconds (free tier: 15, paid: 2–10) |
