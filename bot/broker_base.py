"""
broker_base.py — Abstract interface every broker implementation must satisfy.

All public methods are synchronous from the caller's perspective.
Implementations internally may use REST (OANDA) or Twisted TCP (cTrader).
"""

from abc import ABC, abstractmethod


class BrokerBase(ABC):
    """
    Common interface for all broker connectors.
    Subclass and implement every abstract method.
    """

    # ── Connection ─────────────────────────────────────────────────────────────

    @abstractmethod
    def connect(self, credentials: dict) -> dict:
        """
        Establish broker connection with the given credentials.
        Returns {"ok": bool, "error": str}.
        """

    @abstractmethod
    def disconnect(self):
        """Cleanly close the broker connection."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the connection is live."""

    # ── Account ────────────────────────────────────────────────────────────────

    @abstractmethod
    def get_account_info(self) -> dict:
        """
        Return current account snapshot.
        {
          "balance": float, "equity": float, "margin_used": float,
          "currency": str, "open_trade_count": int,
        }
        """

    # ── Market state ──────────────────────────────────────────────────────────

    @abstractmethod
    def get_price(self, symbol: str) -> dict:
        """
        Return current bid/ask for a symbol.
        {"bid": float, "ask": float, "spread": float}
        """

    # ── Order management ──────────────────────────────────────────────────────

    @abstractmethod
    def place_market_order(self, symbol: str, direction: str,
                            units: float, sl: float, tp: float) -> dict:
        """
        Place a market order.
        direction: "BUY" or "SELL"
        units: broker-native unit (OANDA: units; cTrader: lots)
        Returns {"ok": bool, "order_id": str, "fill_price": float, "error": str}
        """

    @abstractmethod
    def modify_trade(self, order_id: str, sl: float, tp: float) -> dict:
        """
        Modify SL/TP on an open trade.
        Returns {"ok": bool, "error": str}
        """

    @abstractmethod
    def close_trade(self, order_id: str, units: float = None) -> dict:
        """
        Close an open trade fully (or partially if units is given).
        Returns {"ok": bool, "close_price": float, "error": str}
        """

    @abstractmethod
    def get_open_trades(self) -> list:
        """
        Return list of open trade dicts:
        [
          {
            "order_id": str, "symbol": str, "direction": str,
            "units": float, "open_price": float,
            "sl": float, "tp": float, "unrealized_pnl": float,
          }
        ]
        """

    # ── Helpers ───────────────────────────────────────────────────────────────

    @abstractmethod
    def normalize_symbol(self, symbol: str) -> str:
        """Convert bot symbol (EURUSD) to broker format (EUR_USD / EURUSD)."""

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """Human-readable broker name for logging."""
