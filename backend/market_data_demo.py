"""FinAlly — Market Data Live Demo.

A full-screen terminal showcase of the market data subsystem.
Displays live-streaming prices with sparklines, a mock portfolio,
and an event log — giving a taste of the full trading workstation UI.

Run with:  uv run market_data_demo.py
           uv run market_data_demo.py --duration 120
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.market.cache import PriceCache
from app.market.seed_prices import SEED_PRICES
from app.market.simulator import SimulatorDataSource

# ── Constants ────────────────────────────────────────────────────────────────

TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]

# FinAlly brand colours (Rich supports hex in markup)
YELLOW  = "#ecad0a"
BLUE    = "#209dd7"
PURPLE  = "#753991"
DIM     = "bright_black"
UP      = "green"
DOWN    = "red"
FLAT    = DIM

# Sparkline block chars — 8 levels, low → high
SPARK   = "▁▂▃▄▅▆▇█"

# Mock starting positions for the portfolio panel
MOCK_POSITIONS = {
    "AAPL":  {"qty": 15,  "avg_cost": 185.00},
    "NVDA":  {"qty": 5,   "avg_cost": 850.00},
    "MSFT":  {"qty": 10,  "avg_cost": 415.00},
    "GOOGL": {"qty": 8,   "avg_cost": 170.00},
    "TSLA":  {"qty": 12,  "avg_cost": 240.00},
}
CASH = 3_241.50

# Rotating AI insight snippets (shown in footer)
AI_HINTS = [
    "NVDA is your largest position — consider trimming if earnings disappoint.",
    "Tech concentration is 72% of portfolio. Diversification opportunity in JPM.",
    "TSLA showing elevated volatility today. Stop-loss at $220 worth considering.",
    "Portfolio up 4.2% since open. Strong session driven by NVDA and AAPL.",
    "GOOGL approaching 52-week high. Momentum favourable heading into earnings.",
    "Cash reserves at 8%. Deploy into dips or hold for upcoming FOMC meeting?",
    "Correlation between AAPL and MSFT is 0.82 today — effective single-position risk.",
    "META breaking out of 30-day range. Watching for follow-through above $520.",
]


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class TickerStats:
    """Per-ticker session statistics accumulated during the demo."""
    history:    deque = field(default_factory=lambda: deque(maxlen=48))
    session_hi: float = 0.0
    session_lo: float = float("inf")
    last_tick:  float = 0.0  # Unix timestamp of the most recent price update

    def record(self, price: float) -> None:
        self.history.append(price)
        self.last_tick = time.time()
        if price > self.session_hi:
            self.session_hi = price
        if price < self.session_lo:
            self.session_lo = price


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _spark_chars(values: list[float]) -> str:
    """Convert a list of floats to raw sparkline block characters."""
    if not values:
        return ""
    if len(values) == 1:
        return SPARK[len(SPARK) // 2]
    lo, hi = min(values), max(values)
    spread = hi - lo
    n = len(SPARK) - 1
    if spread == 0:
        return SPARK[n // 2] * len(values)
    return "".join(SPARK[round((v - lo) / spread * n)] for v in values)


def sparkline_text(values: list[float], direction: str, last_tick: float) -> Text:
    """Render a live sparkline as a Rich Text with a colored, flashing tail.

    Visual encoding:
      Body  (older ticks) — dim blue: price history context
      Tail  (last 14 chars) — direction color: recent trend
      Tip   (newest tick)  — bold bright green/red, flashes for 500ms on update
    """
    chars = _spark_chars(list(values))
    if not chars:
        return Text("", style=DIM)

    # Split into body / tail / tip
    TAIL_LEN = min(14, max(0, len(chars) - 1))
    body = chars[:len(chars) - TAIL_LEN - 1]
    tail = chars[len(chars) - TAIL_LEN:-1] if TAIL_LEN > 0 else ""
    tip  = chars[-1]

    c = UP if direction == "up" else (DOWN if direction == "down" else FLAT)

    # Flash: tip is bright & bold for 500ms after a new tick arrives
    age = time.time() - last_tick
    if age < 0.5:
        tip_style = f"bold {'bright_green' if direction == 'up' else 'bright_red'}"
    else:
        tip_style = f"bold {c}"

    t = Text(no_wrap=True)
    t.append(body, style=f"{DIM}")
    t.append(tail, style=c)
    t.append(tip,  style=tip_style)
    return t


def fmt_price(price: float) -> str:
    return f"{price:,.2f}"


def direction_color(direction: str) -> str:
    return UP if direction == "up" else (DOWN if direction == "down" else FLAT)


def arrow(direction: str) -> str:
    return "▲" if direction == "up" else ("▼" if direction == "down" else "─")


def pnl_color(value: float) -> str:
    return UP if value > 0 else (DOWN if value < 0 else FLAT)


def connection_dot(elapsed: float) -> Text:
    """Blinking green dot indicating live data."""
    t = Text()
    # Slow blink: on for 800ms, off for 200ms out of every 1000ms
    phase = elapsed % 1.0
    if phase < 0.8:
        t.append("● ", style=f"bold {UP}")
    else:
        t.append("○ ", style=DIM)
    t.append("LIVE", style=f"bold {UP}")
    return t


# ── Panel builders ────────────────────────────────────────────────────────────

def build_header(elapsed: float, remaining: float, total_val: float, cash: float) -> Panel:
    t = Text()
    t.append("  FinAlly ", style=f"bold {YELLOW}")
    t.append("AI Trading Workstation", style="bold white")
    t.append("   │   ", style=DIM)
    t.append(connection_dot(elapsed))
    t.append("   │   ", style=DIM)
    t.append("Portfolio  ", style=DIM)
    t.append(f"${total_val:,.2f}", style=f"bold {BLUE}")
    t.append("   │   ", style=DIM)
    t.append("Cash  ", style=DIM)
    t.append(f"${cash:,.2f}", style="bold white")
    t.append("   │   ", style=DIM)
    t.append(f"{elapsed:5.1f}s", style=DIM)
    t.append(" / ", style=DIM)
    t.append(f"{remaining:.0f}s remaining", style=DIM)
    return Panel(t, border_style=YELLOW, padding=(0, 1))


def build_price_table(cache: PriceCache, stats: dict[str, TickerStats]) -> Panel:
    tbl = Table(
        expand=True,
        box=box.SIMPLE_HEAD,
        border_style=DIM,
        header_style=f"bold {BLUE}",
        padding=(0, 1),
        show_edge=False,
    )
    tbl.add_column("Ticker",   style="bold white",  width=7,  no_wrap=True)
    tbl.add_column("Price",    justify="right",      width=10, no_wrap=True)
    tbl.add_column("Chg",      justify="right",      width=8,  no_wrap=True)
    tbl.add_column("Chg %",    justify="right",      width=8,  no_wrap=True)
    tbl.add_column(" ",        justify="center",     width=2,  no_wrap=True)
    tbl.add_column("Session Hi",  justify="right",   width=10, no_wrap=True)
    tbl.add_column("Session Lo",  justify="right",   width=10, no_wrap=True)
    tbl.add_column("Sparkline ──── 48 ticks ────", width=50,  no_wrap=True)

    for ticker in TICKERS:
        update = cache.get(ticker)
        st = stats.get(ticker)
        if update is None or st is None:
            tbl.add_row(ticker, "…", "…", "…", "", "…", "…", "")
            continue

        c = direction_color(update.direction)
        a = arrow(update.direction)
        spark = sparkline_text(list(st.history), update.direction, st.last_tick)

        hi_str = f"${fmt_price(st.session_hi)}" if st.session_hi > 0 else "─"
        lo_str = f"${fmt_price(st.session_lo)}" if st.session_lo < float('inf') else "─"

        tbl.add_row(
            ticker,
            Text(f"${fmt_price(update.price)}", style=f"bold {c}"),
            Text(f"{update.change:+.2f}",        style=c),
            Text(f"{update.change_percent:+.2f}%", style=c),
            Text(a,                               style=f"bold {c}"),
            Text(hi_str,                          style=UP),
            Text(lo_str,                          style=DOWN),
            spark,
        )

    return Panel(
        tbl,
        title=f"[bold {BLUE}]Watchlist[/]",
        border_style=DIM,
        padding=(0, 0),
    )


def build_portfolio(cache: PriceCache, cash: float) -> Panel:
    """Mock portfolio positions panel."""
    tbl = Table(
        expand=True,
        box=box.SIMPLE_HEAD,
        border_style=DIM,
        header_style=f"bold {PURPLE}",
        padding=(0, 1),
        show_edge=False,
    )
    tbl.add_column("Ticker",     style="bold white", width=7)
    tbl.add_column("Qty",        justify="right",    width=5)
    tbl.add_column("Avg Cost",   justify="right",    width=10)
    tbl.add_column("Current",    justify="right",    width=10)
    tbl.add_column("Unr. P&L",  justify="right",    width=11)
    tbl.add_column("Return",     justify="right",    width=8)

    total_market = cash
    total_cost   = cash

    for ticker, pos in MOCK_POSITIONS.items():
        qty      = pos["qty"]
        avg_cost = pos["avg_cost"]
        update   = cache.get(ticker)
        cur      = update.price if update else avg_cost
        mkt_val  = qty * cur
        cost_val = qty * avg_cost
        pnl      = mkt_val - cost_val
        ret_pct  = (pnl / cost_val) * 100 if cost_val else 0
        total_market += mkt_val
        total_cost   += cost_val

        c = pnl_color(pnl)
        tbl.add_row(
            ticker,
            str(qty),
            f"${fmt_price(avg_cost)}",
            Text(f"${fmt_price(cur)}", style=f"bold {c}"),
            Text(f"${pnl:+,.2f}",    style=c),
            Text(f"{ret_pct:+.2f}%", style=c),
        )

    # Totals row
    total_pnl     = total_market - total_cost
    total_ret_pct = (total_pnl / total_cost) * 100 if total_cost else 0
    tc = pnl_color(total_pnl)

    tbl.add_section()
    tbl.add_row(
        "[bold white]TOTAL[/]",
        "",
        "",
        Text(f"${fmt_price(total_market)}", style=f"bold {YELLOW}"),
        Text(f"${total_pnl:+,.2f}",         style=f"bold {tc}"),
        Text(f"{total_ret_pct:+.2f}%",       style=f"bold {tc}"),
    )

    return Panel(
        tbl,
        title=f"[bold {PURPLE}]Positions[/]",
        border_style=DIM,
        padding=(0, 0),
    )


def build_event_log(events: deque) -> Panel:
    t = Text()
    if not events:
        t.append("Watching for notable moves (±1%) …", style=f"italic {DIM}")
    else:
        for line in events:
            t.append_text(line)
            t.append("\n")
    return Panel(
        t,
        title=f"[bold {YELLOW}]Event Log[/]",
        border_style=DIM,
        padding=(0, 1),
    )


def build_ai_hint(hint: str) -> Panel:
    t = Text()
    t.append("AI  ", style=f"bold {PURPLE}")
    t.append(hint,   style="white")
    return Panel(t, border_style=PURPLE, padding=(0, 1))


# ── Full dashboard ────────────────────────────────────────────────────────────

def build_dashboard(
    cache:      PriceCache,
    stats:      dict[str, TickerStats],
    events:     deque,
    start_time: float,
    duration:   int,
    hint_idx:   int,
) -> Layout:
    elapsed   = time.time() - start_time
    remaining = max(0.0, duration - elapsed)

    # Compute live portfolio total for header
    total_market = CASH
    for ticker, pos in MOCK_POSITIONS.items():
        update = cache.get(ticker)
        cur = update.price if update else pos["avg_cost"]
        total_market += pos["qty"] * cur

    hint = AI_HINTS[hint_idx % len(AI_HINTS)]

    layout = Layout()
    layout.split_column(
        Layout(name="header",    size=3),
        Layout(name="prices",    ratio=3),
        Layout(name="lower",     ratio=2),
        Layout(name="ai_footer", size=3),
    )
    layout["lower"].split_row(
        Layout(name="portfolio", ratio=3),
        Layout(name="events",    ratio=2),
    )

    layout["header"].update(build_header(elapsed, remaining, total_market, CASH))
    layout["prices"].update(build_price_table(cache, stats))
    layout["portfolio"].update(build_portfolio(cache, CASH))
    layout["events"].update(build_event_log(events))
    layout["ai_footer"].update(build_ai_hint(hint))

    return layout


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(duration: int) -> None:
    cache  = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.5)
    stats  = {t: TickerStats() for t in TICKERS}
    events: deque = deque(maxlen=10)

    await source.start(TICKERS)
    start_time = time.time()
    hint_idx   = 0
    hint_timer = time.time()

    # Prime stats with initial prices
    for ticker in TICKERS:
        upd = cache.get(ticker)
        if upd:
            stats[ticker].record(upd.price)

    try:
        with Live(
            build_dashboard(cache, stats, events, start_time, duration, hint_idx),
            refresh_per_second=6,
            screen=True,
        ) as live:
            last_version = -1
            while time.time() - start_time < duration:
                await asyncio.sleep(0.18)

                # Rotate AI hint every 8 seconds
                if time.time() - hint_timer >= 8:
                    hint_idx  += 1
                    hint_timer = time.time()

                if cache.version == last_version:
                    live.update(
                        build_dashboard(cache, stats, events, start_time, duration, hint_idx)
                    )
                    continue
                last_version = cache.version

                # Update stats and detect notable moves
                for ticker in TICKERS:
                    upd = cache.get(ticker)
                    if upd is None:
                        continue
                    stats[ticker].record(upd.price)

                    if abs(upd.change_percent) >= 1.0:
                        c = UP if upd.direction == "up" else DOWN
                        a = arrow(upd.direction)
                        ts = time.strftime("%H:%M:%S")
                        line = Text()
                        line.append(f"{ts}  ", style=DIM)
                        line.append(f"{a} {ticker:<6}", style=f"bold {c}")
                        line.append(f"  {upd.change_percent:+.2f}%", style=c)
                        line.append(f"  ${fmt_price(upd.price)}", style="white")
                        events.appendleft(line)

                live.update(
                    build_dashboard(cache, stats, events, start_time, duration, hint_idx)
                )

    except KeyboardInterrupt:
        pass
    finally:
        await source.stop()

    _print_summary(cache, stats, start_time)


def _print_summary(cache: PriceCache, stats: dict[str, TickerStats], start_time: float) -> None:
    console = Console()
    console.print()
    console.print(f"[bold {YELLOW}]  FinAlly[/] [bold white]Session Summary[/]  "
                  f"[{DIM}]{time.time() - start_time:.0f}s[/]")
    console.print()

    tbl = Table(
        box=box.SIMPLE_HEAD,
        border_style=DIM,
        header_style=f"bold {BLUE}",
        padding=(0, 2),
    )
    tbl.add_column("Ticker",   style="bold white", width=7)
    tbl.add_column("Seed",     justify="right",    width=10)
    tbl.add_column("Final",    justify="right",    width=10)
    tbl.add_column("Session Hi",  justify="right", width=11)
    tbl.add_column("Session Lo",  justify="right", width=11)
    tbl.add_column("Return",   justify="right",    width=9)

    for ticker in TICKERS:
        seed  = SEED_PRICES.get(ticker, 0.0)
        upd   = cache.get(ticker)
        st    = stats[ticker]
        if upd is None:
            continue
        final = upd.price
        ret   = ((final - seed) / seed) * 100 if seed else 0.0
        c     = pnl_color(ret)

        tbl.add_row(
            ticker,
            f"${fmt_price(seed)}",
            Text(f"${fmt_price(final)}", style=f"bold {c}"),
            Text(f"${fmt_price(st.session_hi)}", style=UP),
            Text(f"${fmt_price(st.session_lo)}", style=DOWN),
            Text(f"{ret:+.2f}%",                 style=f"bold {c}"),
        )

    console.print(tbl)
    console.print(f"[{DIM}]  Ctrl+C or --duration to control run length.[/]")
    console.print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="FinAlly market data live demo")
    parser.add_argument(
        "--duration", "-d",
        type=int,
        default=60,
        help="How long to run the demo in seconds (default: 60)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.duration))


if __name__ == "__main__":
    main()
