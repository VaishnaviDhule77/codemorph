"""Sample legacy calculator module used in CodeMorph tests.

It exercises every Phase-1 analyzer capability: functions, methods,
nested functions, imports, loops, conditionals, exceptions and
internal call dependencies.
"""

import math
from typing import List, Optional


DISCOUNT_THRESHOLD = 100.0
TAX_RATE = 0.2


def validate_amount(amount: float) -> float:
    """Return the amount if valid, raising otherwise."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    return amount


def calculate_tax(amount: float, rate: float = TAX_RATE) -> float:
    """Compute the tax owed on *amount*."""
    return amount * rate


def calculate_total(amount: float, items: Optional[List[str]] = None) -> float:
    """Add tax and apply a volume discount."""
    validated = validate_amount(amount)
    tax = calculate_tax(validated)
    total = validated + tax
    if items is None:
        items = []
    for item in items:
        if len(item) > 3:
            total += DISCOUNT_THRESHOLD * 0.01
    try:
        receipt = math.fsum([total, 0.0])
    except (TypeError, ValueError):
        receipt = total
    return receipt


class Calculator:
    """A tiny calculator with a nested helper."""

    def __init__(self, precision: int = 2):
        self.precision = precision

    def add(self, a: float, b: float) -> float:
        """Return a + b, clamped to positive values."""
        result = a + b
        if result < 0:
            return 0.0
        return round(result, self.precision)

    def _run_nested(self, values):
        def clamp(value):
            if value < 0:
                return 0
            return value
        return [clamp(v) for v in values]


def main() -> None:
    calc = Calculator(precision=3)
    total = calculate_total(10.0, ["book", "pen"])
    print("total:", total, "sum:", calc.add(total, 1))
    for i in range(3):
        print("tick", i)


if __name__ == "__main__":
    main()