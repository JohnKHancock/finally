# FinAlly — Project Summary

**Date:** 2026-04-10  
**Status:** Market data subsystem complete. Backend API, frontend, LLM integration, and Docker/deployment not yet built.

---

## 1. What FinAlly Is

FinAlly (Finance Ally) is an AI-powered trading workstation: a Bloomberg-style dark-theme single-page app where users watch live (simulated) stock prices, trade a virtual $10,000 portfolio, and interact with an LLM assistant that can analyze positions and execute trades on their behalf.

It runs as a single Docker container on port 8000. No login, no signup — users hit `http://localhost:8000` and immediately see prices streaming.

See [`planning/PLAN.md`](planning/PLAN.md) for the full specification.

---

## 2. Architecture

```
Docker Container (port 8000)
├── FastAPI (Python/uv)
│   ├── /api/*           REST endpoints (portfolio, watchlist, chat)
│   ├── /api/stream/*    SSE price streaming
│   └── /*               Static file serving (Next.js export)
├── SQLite (db/finally.db, volume-mounted)
└── Background tasks:
    ├── Market data simulator (GBM) or Massive/Polygon.io poller
    └── Portfolio snapshot recorder (every 30s)
```

**Key choices:** SSE over WebSockets (simpler, one-way push), static Next.js export (single origin, no CORS), SQLite (no multi-user, zero config), `uv` for Python project management.

---

## 3. Build Status

### Complete

| Component | Location | Status |
|-----------|----------|--------|
| Market data subsystem | `backend/app/market/` | ✅ Complete, 216 tests passing |

### Not Yet Built

| Component | Notes |
|-----------|-------|
| FastAPI application entrypoint | Lifecycle mgmt, DB init, router wiring |
| Database layer | SQLite schema, seed data, lazy init |
| Portfolio API | `/api/portfolio`, `/api/portfolio/trade`, `/api/portfolio/history` |
| Watchlist API | `/api/watchlist` CRUD |
| Chat/LLM API | `/api/chat` — LiteLLM → OpenRouter → Cerebras |
| Frontend | Next.js + TypeScript static export |
| Docker build | Multi-stage Dockerfile |
| Start/stop scripts | `scripts/` for macOS and Windows |
| E2E tests | Playwright in `test/` with `docker-compose.test.yml` |

---

## 4. Market Data Subsystem

**Location:** `backend/app/market/`  
**Status:** Complete and reviewed. All known issues resolved.

### Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource  — GBM simulator (default, no API key required)
└── MassiveDataSource    — Polygon.io REST poller (when MASSIVE_API_KEY is set)
        │
        ▼ writes every ~500ms
   PriceCache (thread-safe, in-memory)
        │
        ├──→ SSE endpoint  GET /api/stream/prices
        ├──→ Portfolio valuation
        └──→ Trade execution
```

### Modules

| File | Responsibility |
|------|---------------|
| `models.py` | `PriceUpdate` — frozen dataclass with ticker, price, previous_price, timestamp, plus computed change/direction/to_dict() |
| `interface.py` | `MarketDataSource` — ABC defining start/stop/add_ticker/remove_ticker/get_tickers |
| `cache.py` | `PriceCache` — thread-safe store with version counter for SSE change detection |
| `seed_prices.py` | Realistic seed prices and per-ticker GBM drift/volatility params; sector correlation groups |
| `simulator.py` | `GBMSimulator` — GBM with Cholesky-correlated sector moves + random shock events; `SimulatorDataSource` wraps it |
| `massive_client.py` | `MassiveDataSource` — polls Polygon.io REST API in a thread pool to avoid blocking the event loop |
| `factory.py` | `create_market_data_source(cache)` — reads `MASSIVE_API_KEY` at call time; returns the appropriate implementation |
| `stream.py` | `create_stream_router(cache)` — FastAPI factory returning an `APIRouter` with `GET /api/stream/prices` SSE endpoint |
| `__init__.py` | Re-exports public API: `PriceCache`, `PriceUpdate`, `MarketDataSource`, `create_market_data_source`, `create_stream_router` |

### GBM Simulator Details

- Uses exact discrete-time GBM: `S * exp((μ - 0.5σ²)Δt + σ√Δt · Z)`
- Correlated moves via Cholesky decomposition of a sector-based correlation matrix (tech: 0.6, finance: 0.5, cross-sector: 0.3)
- ~0.1% chance per tick per ticker of a random 2–5% shock event for visual drama
- Updates at ~500ms intervals; starts from realistic seed prices (AAPL ~$190, NVDA ~$875, etc.)

### Default Tickers

AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

### Usage (for downstream backend code)

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# Startup (FastAPI lifespan)
cache = PriceCache()
source = create_market_data_source(cache)   # reads MASSIVE_API_KEY env var
await source.start(["AAPL", "GOOGL", ...])
router = create_stream_router(cache)        # mount on app

# Read prices anywhere
update = cache.get("AAPL")          # PriceUpdate | None
price  = cache.get_price("AAPL")    # float | None
all_px = cache.get_all()            # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("PYPL")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```

---

## 5. Test Suite

**216 tests, all passing.** Located in `backend/tests/market/`.

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -v              # Run all tests
uv run --extra dev pytest --cov=app       # With coverage report
uv run --extra dev ruff check app/ tests/ # Lint
```

| Module | Tests | Coverage |
|--------|-------|----------|
| test_models.py | 11 | 100% |
| test_cache.py | 13 | 100% |
| test_simulator.py | 17 | 98% |
| test_simulator_source.py | 10 | integration |
| test_factory.py | 7 | 100% |
| test_massive.py | 13 | 94% |
| test_interface.py | varies | 100% |
| test_seed_prices.py | varies | 100% |
| test_stream.py | varies | 92% |

Overall coverage: ~97%.

### Known Acceptable Gaps

- `stream.py` lines 39, 87–88 — `StreamingResponse` return and `CancelledError` path require ASGI test client or task cancellation; not covered by unit tests.
- `massive_client.py` lines 85–87, 125 — poll sleep interval and direct `_fetch_snapshots` call require live network.

---

## 6. Open Issues (from Code Review)

The `planning/MARKET_DATA_REVIEW.md` documents issues found in the second review pass. **All high-severity issues have been resolved** (216 tests passing). Remaining items are low-severity:

| Severity | Issue | Location |
|----------|-------|----------|
| Low | `CancelledError` is logged and re-raised correctly — confirm `raise` is present | `stream.py:88` |
| Low | `add_ticker` silently no-ops when called before `start()` — no error or warning | `simulator.py:246` |
| Low | `conftest.py` `event_loop_policy` fixture returns a policy but never applies it — dead code | `tests/conftest.py` |
| Low | TSLA listed in `tech` correlation group but uses `CROSS_GROUP_CORR` (0.3) — misleading but correct | `seed_prices.py` |
| Low | Residual lint warnings may exist in test files — run `ruff check --fix tests/` to confirm | `tests/market/` |

---

## 7. Environment Variables

```bash
# Required for LLM chat functionality
OPENROUTER_API_KEY=your-key-here

# Optional: real market data via Polygon.io; simulator used if absent
MASSIVE_API_KEY=

# Optional: deterministic mock LLM responses for E2E tests
LLM_MOCK=false
```

`.env` is gitignored; `.env.example` is committed.

---

## 8. Database Schema (to be implemented)

Six tables, all with a `user_id TEXT DEFAULT "default"` column for future multi-user support:

| Table | Purpose |
|-------|---------|
| `users_profile` | Cash balance per user |
| `watchlist` | Tickers being watched |
| `positions` | Current holdings (one row per ticker per user) |
| `trades` | Append-only trade log |
| `portfolio_snapshots` | Total portfolio value recorded every 30s and after each trade |
| `chat_messages` | LLM conversation history including executed actions as JSON |

The backend lazily initializes the database on first request — no manual migration step.

---

## 9. LLM Integration (to be implemented)

- Uses LiteLLM → OpenRouter → `openrouter/openai/gpt-oss-120b` with Cerebras inference
- Structured output schema: `{ message, trades[], watchlist_changes[] }`
- Auto-executes trades from LLM response (simulated money, no confirmation needed)
- `LLM_MOCK=true` returns deterministic responses for E2E testing
- Context injected per request: cash balance, positions with P&L, watchlist prices, recent chat history

---

## 10. Demo

A Rich terminal dashboard is available to verify the market data subsystem:

```bash
cd backend
uv run market_data_demo.py
```

Displays all 10 tickers with live prices, sparklines, color-coded direction arrows, and a shock-event log. Runs 60 seconds or until Ctrl+C.

---

## 11. Key Documents

| Document | Purpose |
|----------|---------|
| [`planning/PLAN.md`](planning/PLAN.md) | Full project specification (authoritative) |
| [`planning/MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md) | Market data implementation summary |
| [`planning/MARKET_DATA_DESIGN.md`](planning/MARKET_DATA_DESIGN.md) | Detailed market data design & implementation guide |
| [`planning/MARKET_DATA_REVIEW.md`](planning/MARKET_DATA_REVIEW.md) | Code review findings (second pass) |
| [`backend/CLAUDE.md`](backend/CLAUDE.md) | Backend developer quick-start guide |
| [`backend/README.md`](backend/README.md) | Backend structure and commands |
