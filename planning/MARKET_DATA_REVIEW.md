# Market Data Backend — Code Review

**Reviewer:** Claude Code (Sonnet 4.6)  
**Date:** 2026-04-10  
**Scope:** `backend/app/market/` (8 modules) + `backend/tests/market/` (9 test files)

---

## Test Run Results

```
211 tests collected
210 passed, 1 failed — 2.47s
Coverage: 97% overall
```

| Module | Stmts | Miss | Cover |
|---|---|---|---|
| app/market/models.py | 36 | 0 | 100% |
| app/market/cache.py | 47 | 0 | 100% |
| app/market/interface.py | 13 | 0 | 100% |
| app/market/factory.py | 15 | 0 | 100% |
| app/market/seed_prices.py | 8 | 0 | 100% |
| app/market/simulator.py | 141 | 3 | 98% |
| app/market/massive_client.py | 67 | 4 | 94% |
| app/market/stream.py | 36 | 3 | 92% |

---

## Linter Results

`ruff check app/ tests/` found **10 errors** in test files. These were listed as fixed in the previous review summary (`MARKET_DATA_SUMMARY.md`) but remain unfixed.

| File | Rule | Issue |
|---|---|---|
| `test_massive.py:3` | F401 | `AsyncMock` imported but unused |
| `test_seed_prices.py:3` | F401 | `pytest` imported but unused |
| `test_seed_prices.py:3` | I001 | Import block unsorted |
| `test_simulator.py:7` | F401 | `TICKER_PARAMS` imported but unused |
| `test_simulator.py:3` | I001 | Import block unsorted |
| `test_simulator.py:190` | N806 | Variable `L` should be lowercase |
| `test_simulator_source.py:9` | F401 | `GBMSimulator` imported but unused |
| `test_simulator_source.py:3` | I001 | Import block unsorted |
| `test_stream.py:3` | F401 | `asyncio` imported but unused |
| `test_stream.py:5` | F401 | `AsyncGenerator` imported but unused |

All 9 of the fixable ones can be resolved with `ruff check --fix`.

---

## Issues Found

### HIGH: Flaky Test — Wrong Metric for Volatility Comparison

**File:** `tests/market/test_simulator.py:247`  
**Test:** `TestGBMSimulatorParameters::test_high_volatility_ticker_more_variable`

This test **fails on most runs** (confirmed: failed twice during review). The failure is a design flaw in the test, not a bug in the code.

**The problem:** The test compares `np.std(price_series)` for TSLA vs V over 10,000 steps. This measures the spread of the price *level* across the time series — dominated by drift direction, not volatility. With only ~83 simulated trading minutes of data, V (starting at $280) can easily produce a higher absolute price std than TSLA (starting at $250) purely by chance of drift direction.

**The correct metric is standard deviation of log-returns**, which directly measures per-step volatility and scales with sigma as intended:

```python
def test_high_volatility_ticker_more_variable(self):
    """TSLA (sigma=0.5) should have higher per-step volatility than V (sigma=0.17)."""
    import random
    random.seed(42)
    np.random.seed(42)

    n_steps = 10_000
    tsla_sim = GBMSimulator(tickers=["TSLA"])
    v_sim = GBMSimulator(tickers=["V"])

    tsla_log_returns = []
    v_log_returns = []

    prev_tsla = tsla_sim.get_price("TSLA")
    prev_v = v_sim.get_price("V")

    for _ in range(n_steps):
        t = tsla_sim.step()["TSLA"]
        v = v_sim.step()["V"]
        tsla_log_returns.append(math.log(t / prev_tsla))
        v_log_returns.append(math.log(v / prev_v))
        prev_tsla, prev_v = t, v

    tsla_std = float(np.std(tsla_log_returns))
    v_std = float(np.std(v_log_returns))
    assert tsla_std > v_std, f"TSLA log-return std {tsla_std:.6f} should > V {v_std:.6f}"
```

This will pass deterministically with sufficient n_steps (expected ratio ~3x).

---

### MEDIUM: Timestamp Falsy Check Bug in `cache.py`

**File:** `app/market/cache.py:31`

```python
ts = timestamp or time.time()
```

If `timestamp=0.0` is passed (a valid Unix timestamp, albeit representing 1970-01-01), the falsy check treats it as `None` and substitutes `time.time()`. The correct guard:

```python
ts = timestamp if timestamp is not None else time.time()
```

In practice, timestamps are always recent Unix seconds (1.7B+), so this never triggers — but it is semantically incorrect.

---

### MEDIUM: Inconsistent Ticker Normalization Across Implementations

`MassiveDataSource.add_ticker` normalizes input:
```python
ticker = ticker.upper().strip()
```

`SimulatorDataSource.add_ticker` does not:
```python
async def add_ticker(self, ticker: str) -> None:
    if self._sim:
        self._sim.add_ticker(ticker)  # raw ticker passed through
```

A caller using `SimulatorDataSource` can create duplicate entries like `"aapl"` and `"AAPL"` as separate tickers. Since upstream API callers should normalize, this is low risk in practice, but the interface contract should be consistent. Normalization belongs in the interface implementation or the base class.

---

### MEDIUM: Lint Errors Marked as Fixed But Weren't

`MARKET_DATA_SUMMARY.md` states under "Code Review & Fixes Applied":
> **7. Massive test mocks fixed** — `source._client` set in tests, patches target correct names  
> **6. Unused test imports removed** — `pytest`, `math`, `asyncio` cleaned from 4 test files

The linter confirms these unused imports still exist (`pytest` in `test_seed_prices.py`, `asyncio` in `test_stream.py` and `test_simulator_source.py`, etc.). The summary document is inaccurate.

---

### LOW: `CancelledError` Not Re-Raised in `_generate_events`

**File:** `app/market/stream.py:87-88`

```python
except asyncio.CancelledError:
    logger.info("SSE stream cancelled for: %s", client_ip)
    # Missing: raise
```

The `CancelledError` is swallowed rather than re-raised. For an async generator this may work in practice (the generator simply terminates), but it violates asyncio convention — tasks awaiting cancellation should propagate `CancelledError` upward so the event loop can properly clean up. The coverage gap on lines 87-88 also indicates this path is untested.

Fix: add `raise` after the log line.

---

### LOW: Silent No-Op When `add_ticker`/`remove_ticker` Called Before `start()`

**File:** `app/market/simulator.py:246-253`

```python
async def add_ticker(self, ticker: str) -> None:
    if self._sim:  # None before start()
        ...
```

If called before `start()`, the ticker is silently discarded. The `start()` docstring says "Must be called exactly once. Calling start() twice is undefined behavior" — but the pre-start behavior of `add_ticker` is unspecified. A `RuntimeError` or at minimum a warning log would be safer than silent data loss.

---

### LOW: `conftest.py` Fixture Is Ineffective

**File:** `tests/conftest.py`

```python
@pytest.fixture
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()
```

This fixture returns a policy object but never *applies* it to asyncio (e.g. via `asyncio.set_event_loop_policy()`). With `asyncio_mode = "auto"` set in `pyproject.toml`, pytest-asyncio manages the event loop automatically; this fixture has no effect and is dead code.

---

### LOW: Uncovered Lines — Legitimate Gaps

The following uncovered lines (`simulator.py:153`, `stream.py:39`, `stream.py:87-88`, `massive_client.py:85-87,125`) represent meaningful paths that are difficult to exercise:

- `simulator.py:153` — `_add_ticker_internal` early return when ticker already present. Easy to cover with a test that calls it directly.
- `stream.py:39` — `StreamingResponse` return in the route handler. Requires ASGI test client (e.g. `httpx.AsyncClient` with `TestClient`) rather than testing `_generate_events` directly.
- `stream.py:87-88` — `CancelledError` path in `_generate_events`. Needs an asyncio task to be cancelled mid-stream.
- `massive_client.py:85-87,125` — `_poll_loop` sleep interval and `_fetch_snapshots` direct call. Both require the live network client.

These are acceptable gaps given the external dependency constraints.

---

## Architecture Assessment

**Strengths:**

- **Strategy pattern is clean.** `MarketDataSource` ABC enforces a consistent interface. Both implementations are substitutable. Downstream code is genuinely source-agnostic.
- **GBM math is correct.** The formula `S * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)` is the exact discrete-time GBM solution, not an Euler approximation. Cholesky decomposition for correlated moves is the correct approach.
- **PriceCache thread safety is sound.** A single `threading.Lock` guards all state mutations. The version counter is atomic under the lock. `get_all()` returns a shallow copy (safe for concurrent readers).
- **MassiveDataSource runs synchronous SDK in thread pool.** `asyncio.to_thread(self._fetch_snapshots)` correctly avoids blocking the event loop for the synchronous Polygon.io client.
- **SSE version-based change detection** avoids sending duplicate events when no prices have changed. Efficient.
- **Factory reads env at call time**, not import time — correct, avoids import-order issues with test patching.
- **Error resilience in loops.** Both `_run_loop` and `_poll_once` catch and log exceptions without crashing, with the important distinction that `CancelledError` (a `BaseException`) propagates correctly through `except Exception`.

**Design note — TSLA grouping:** TSLA is in `CORRELATION_GROUPS["tech"]` but has the same correlation as cross-sector stocks (`TSLA_CORR = CROSS_GROUP_CORR = 0.3`). The `_pairwise_correlation` method's TSLA check runs before the group lookup, so this works correctly — but having TSLA in the tech set at all is misleading. It could be removed from `tech` with no behavioral change.

---

## Summary

| Severity | Count | Items |
|---|---|---|
| High | 1 | Flaky volatility test (`test_high_volatility_ticker_more_variable`) |
| Medium | 3 | Falsy timestamp bug, inconsistent ticker normalization, inaccurate fix summary |
| Low | 4 | CancelledError not re-raised, silent pre-start no-op, dead conftest fixture, lint errors |

**The implementation is production-quality.** The architecture, GBM math, thread safety, and SSE streaming are all well-designed. The one failing test is a test bug (wrong metric), not a code bug. The medium and low issues are real but none are blockers for downstream development. Fix the flaky test before proceeding — it will become noise in CI.

### Recommended actions before next phase:

1. Fix `test_high_volatility_ticker_more_variable` to use log-return std and a fixed seed (**required** — it fails on CI)
2. Run `ruff check --fix tests/` to clear the 9 auto-fixable lint errors
3. Fix `ts = timestamp or time.time()` → `ts = timestamp if timestamp is not None else time.time()`
4. Add `raise` after the `CancelledError` log in `stream.py`
