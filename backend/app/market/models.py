"""Data models for market data."""

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

    def __post_init__(self) -> None:
        """Validate fields after construction."""
        if not self.ticker or not self.ticker.strip():
            raise ValueError("ticker must be a non-empty string")
        if self.price < 0:
            raise ValueError(f"price must be non-negative, got {self.price}")
        if self.previous_price < 0:
            raise ValueError(f"previous_price must be non-negative, got {self.previous_price}")

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

    @classmethod
    def from_dict(cls, data: dict) -> PriceUpdate:
        """Deserialize from a dictionary (e.g., from JSON or SSE payload)."""
        return cls(
            ticker=data["ticker"],
            price=data["price"],
            previous_price=data["previous_price"],
            timestamp=data["timestamp"],
        )
