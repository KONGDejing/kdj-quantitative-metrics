"""User-approved commission estimate for new trades and cash plans.

This is deliberately a hybrid planning rule, not a claim about the broker's
exact settlement: small trades use 5 yuan per lot, and larger trades use
0.03% of the whole execution amount. Explicit historical trade fees win.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


LOT_SIZE = 100
SMALL_TRADE_CUTOFF = Decimal("16700")
LARGE_TRADE_RATE = Decimal("0.0003")


def estimate_trade_fee(price: float, lots: float, *, fee_per_lot: float = 5.0) -> float:
    """Estimate one execution's commission using the confirmed hybrid rule."""
    if price <= 0 or lots <= 0:
        return 0.0
    amount = Decimal(str(price)) * Decimal(str(lots)) * LOT_SIZE
    if amount <= SMALL_TRADE_CUTOFF:
        fee = Decimal(str(fee_per_lot)) * Decimal(str(lots))
    else:
        fee = amount * LARGE_TRADE_RATE
    return float(fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
