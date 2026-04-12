# Massive API Research (formerly Polygon.io)

## Overview

Polygon.io rebranded as **Massive** (massive.com) in October 2025. Existing API keys, endpoints at
`api.polygon.io`, and all integrations continue to work. The new base URL is `api.massive.com`.
Both base URLs are supported during the transition period.

- **Docs**: https://massive.com/docs
- **Stocks REST overview**: https://massive.com/docs/rest/stocks/overview
- **Python client**: `pip install -U massive` (requires Python 3.9+)

---

## Authentication

All requests require an API key, passed either as a query parameter or HTTP header:

```
# Query param
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?apiKey=YOUR_KEY

# Header (preferred)
Authorization: Bearer YOUR_KEY
```

---

## Key Endpoints

### 1. Multi-Ticker Snapshot (Primary endpoint for this project)

**`GET /v2/snapshot/locale/us/markets/stocks/tickers`**

Returns the current snapshot (last trade, last quote, current day bar, previous day bar, and
minute bar) for a set of tickers, or the entire market if no tickers specified.

**Query Parameters:**

| Parameter   | Type    | Required | Description |
|-------------|---------|----------|-------------|
| `tickers`   | string  | No       | Comma-separated list of tickers (case-sensitive). Omit for all ~10,000+ tickers. |
| `include_otc` | boolean | No   | Include OTC securities (default: false) |

**Example Request:**
```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT&apiKey=YOUR_KEY
```

**Example Response:**
```json
{
  "status": "OK",
  "count": 3,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.23,
      "todaysChangePerc": 0.65,
      "updated": 1672754400000000000,
      "day": {
        "o": 189.50,
        "h": 191.20,
        "l": 188.90,
        "c": 190.45,
        "v": 54321000,
        "vw": 189.85
      },
      "min": {
        "o": 190.30,
        "h": 190.50,
        "l": 190.10,
        "c": 190.45,
        "v": 145000,
        "vw": 190.38,
        "t": 1672754400000
      },
      "prevDay": {
        "o": 188.10,
        "h": 190.00,
        "l": 187.50,
        "c": 189.22,
        "v": 61000000,
        "vw": 188.80
      },
      "lastTrade": {
        "p": 190.45,
        "s": 100,
        "t": 1672754399000000000,
        "c": [14, 41]
      },
      "lastQuote": {
        "P": 190.46,
        "S": 3,
        "p": 190.45,
        "s": 2
      }
    }
  ]
}
```

**Response field reference:**
- `day.c` — current day close (most recent price during market hours)
- `min.c` — close of the most recent minute bar
- `lastTrade.p` — price of the most recent individual trade
- `todaysChangePerc` — % change from previous close
- `updated` — nanosecond timestamp of last update

**Best practice for current price**: use `lastTrade.p` if available, fall back to `min.c`, then `day.c`.

---

### 2. Unified Snapshot (v3, multi-asset class)

**`GET /v3/snapshot`**

Newer endpoint covering stocks, options, forex, crypto, and indices in one call. Supports filtering
by ticker with `ticker.any_of` (up to 250 tickers).

**Query Parameters:**

| Parameter       | Type    | Description |
|-----------------|---------|-------------|
| `ticker.any_of` | string  | Comma-separated tickers (max 250) |
| `type`          | string  | Asset class filter: `stocks`, `options`, `fx`, `crypto`, `indices` |
| `limit`         | integer | Results per page (default: 10, max: 250) |
| `order`         | string  | Sort direction |

**Example Request:**
```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&type=stocks&limit=50&apiKey=YOUR_KEY
```

**Example Response:**
```json
{
  "status": "OK",
  "request_id": "abc123",
  "results": [
    {
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "type": "CS",
      "market_status": "open",
      "session": {
        "open": 189.50,
        "close": 190.45,
        "high": 191.20,
        "low": 188.90,
        "volume": 54321000,
        "change": 1.23,
        "change_percent": 0.65,
        "previous_close": 189.22
      },
      "last_trade": {
        "price": 190.45,
        "size": 100,
        "timestamp": 1672754399000000000
      },
      "last_quote": {
        "ask": 190.46,
        "bid": 190.44,
        "ask_size": 3,
        "bid_size": 2
      }
    }
  ],
  "next_url": "https://api.massive.com/v3/snapshot?cursor=..."
}
```

**Note:** v3 uses `next_url` for pagination. For watchlists of ≤250 tickers, a single request suffices.
Prefer v3 for new integrations due to the cleaner response schema.

---

### 3. Single Ticker Snapshot

**`GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}`**

Same data as the multi-ticker endpoint but for one ticker. Useful for targeted lookups.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/AAPL?apiKey=YOUR_KEY
```

Response wraps data in a `ticker` object (not `tickers` array) with identical field structure.

---

### 4. End-of-Day Open/Close

**`GET /v1/open-close/{stocksTicker}/{date}`**

Returns the official OHLCV for a specific trading date. Includes pre-market and after-hours prices.

**Parameters:**

| Parameter     | Type    | Required | Description |
|---------------|---------|----------|-------------|
| `stocksTicker` | string | Yes      | Ticker symbol |
| `date`        | string  | Yes      | Format: `YYYY-MM-DD` |
| `adjusted`    | boolean | No       | Adjusted for splits (default: true) |

**Example Request:**
```
GET /v1/open-close/AAPL/2024-01-09?adjusted=true&apiKey=YOUR_KEY
```

**Example Response:**
```json
{
  "symbol": "AAPL",
  "from": "2024-01-09",
  "status": "OK",
  "open": 185.00,
  "high": 188.44,
  "low": 184.30,
  "close": 187.15,
  "volume": 58234000,
  "preMarket": 184.80,
  "afterHours": 187.50
}
```

---

### 5. Aggregate Bars (OHLCV over time)

**`GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}`**

Returns historical OHLCV bars at configurable granularity. Useful for building sparkline data or
seeding chart history.

**Example — 1-minute bars for AAPL over the past day:**
```
GET /v2/aggs/ticker/AAPL/range/1/minute/2024-01-09/2024-01-09?adjusted=true&apiKey=YOUR_KEY
```

**Example — 1-day bars for the past 30 days:**
```
GET /v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-01-31?adjusted=true&apiKey=YOUR_KEY
```

**Response:**
```json
{
  "ticker": "AAPL",
  "status": "OK",
  "resultsCount": 30,
  "results": [
    {
      "t": 1672531200000,
      "o": 185.00,
      "h": 188.44,
      "l": 184.30,
      "c": 187.15,
      "v": 58234000,
      "vw": 186.50,
      "n": 412345
    }
  ]
}
```

---

## Python Client

### Installation

```bash
pip install -U massive
```

### Basic Usage

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_API_KEY")
```

### Get Current Prices for Multiple Tickers

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_API_KEY")

# Multi-ticker snapshot — the primary polling call for this project
tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"]
snapshot = client.get_snapshot_all_tickers(
    locale="us",
    market_type="stocks",
    tickers=",".join(tickers)
)

for t in snapshot.tickers:
    price = t.last_trade.price if t.last_trade else t.min.c if t.min else t.day.c
    print(f"{t.ticker}: ${price:.2f}  ({t.todays_change_perc:+.2f}%)")
```

### Get Last Trade (single ticker)

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"AAPL last trade: ${trade.results.price}")
```

### Get Aggregate Bars

```python
# List all minute bars for a date range — client handles pagination automatically
bars = []
for agg in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="minute",
    from_="2024-01-09",
    to="2024-01-09",
    limit=50000,
):
    bars.append(agg)

# For daily bars
daily = []
for agg in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2024-01-01",
    to="2024-01-31",
):
    daily.append(agg)
```

### Get End-of-Day Open/Close

```python
eod = client.get_daily_open_close_agg(stocksTicker="AAPL", date="2024-01-09", adjusted=True)
print(f"AAPL close on 2024-01-09: ${eod.close}")
```

### Direct HTTP (no client library)

For environments where the library isn't available, use `httpx` or `requests`:

```python
import httpx

BASE_URL = "https://api.massive.com"

async def get_snapshots(tickers: list[str], api_key: str) -> dict:
    url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {
        "tickers": ",".join(tickers),
        "apiKey": api_key,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        return resp.json()
```

---

## Rate Limits and Tier Guidance

| Plan       | Rate Limit         | Recommended Poll Interval |
|------------|--------------------|---------------------------|
| Free       | 5 requests/minute  | 15 seconds                |
| Starter    | Unlimited          | 5–10 seconds              |
| Developer  | Unlimited          | 2–5 seconds               |
| Advanced+  | Unlimited          | 1–2 seconds               |

**For this project (free tier default):**
- Poll `/v2/snapshot/locale/us/markets/stocks/tickers` with all watchlist tickers in a single request
- Poll every 15 seconds to stay within 5 req/min (4 req/min leaves a safety margin)
- One request fetches all tickers — no need for per-ticker calls

**For paid tiers:**
- Configurable via `MASSIVE_POLL_INTERVAL_SECONDS` env var (default: 15)
- A single multi-ticker snapshot call covers all watchlist tickers simultaneously

---

## Market Hours

- **Pre-market**: 4:00 AM – 9:30 AM ET
- **Regular hours**: 9:30 AM – 4:00 PM ET
- **After-hours**: 4:00 PM – 8:00 PM ET
- **Closed**: weekends and US market holidays

The snapshot endpoint returns `lastTrade` data during and immediately after market hours. During
extended hours, `day.c` may not reflect the most current price — use `lastTrade.p` when available.

For a simulator-only project, market hours are irrelevant: the simulator runs 24/7.

---

## Error Handling

The API uses standard HTTP status codes:

| Code | Meaning |
|------|---------|
| 200  | Success |
| 400  | Bad request (invalid parameters) |
| 403  | Authentication failure (bad/missing API key) |
| 404  | Ticker not found |
| 429  | Rate limit exceeded |
| 500  | Server error |

Failed ticker lookups within a batch (e.g., invalid ticker in the tickers list) return an `error`
field on that result object rather than failing the whole request.

```python
for t in snapshot.tickers:
    if hasattr(t, 'error'):
        print(f"Failed to get data for {t.ticker}: {t.error}")
        continue
    # process valid ticker...
```
