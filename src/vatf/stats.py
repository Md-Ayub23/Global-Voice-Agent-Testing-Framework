"""How sure a number is: likely ranges for rates, and how many samples a percentile needs.

A run makes few calls (a handful of test cases, 3 repeats each), so a rate like "30 of 30 passed" is
not "100%": it says the real rate is very likely above 88%. Every rate in a report carries that range,
and a percentile that rests on too few values is shown, never used to decide.

  wilson(k, n)            -> (low, high): the 95% Wilson score interval of k successes in n tries
  rate(k, n)              -> {k, n, rate, low, high, text}: a rate with its range, in plain words
  enough(p, n)            -> True when n values are enough to trust a p-th percentile
  fisher_exact(a, b, c, d) -> two-sided p-value: are two pass rates really different?

Wilson, not the textbook "rate +- 2 standard errors": that one gives ranges past 0% and 100% and a
zero-width range for 0 or n successes, exactly the cases a release test sees most.
"""

from __future__ import annotations

import math
from typing import Any

Z95 = 1.959964

# The fewest values a percentile needs before it can decide anything. With fewer, the nearest-rank
# p95 is simply one of the slowest few values (of 12 answers, it is the slowest), which is luck.
MIN_FOR = {50: 1, 90: 10, 95: 20, 99: 100}


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n tries, as fractions (0..1). n = 0 gives (0, 1)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    low, high = max(0.0, centre - half), min(1.0, centre + half)
    # Exact at the edges: k = 0 can't have a low above 0, k = n can't have a high below 1.
    return (0.0 if k == 0 else low), (1.0 if k == n else high)


def _pct(x: float, up: bool = False) -> str:
    """A percent for a range edge, rounded outwards (low edges down, high edges up) so the words never
    claim more certainty than the numbers."""
    v = math.ceil(100 * x - 1e-9) if up else math.floor(100 * x + 1e-9)
    return f"{v}%"


def rate(k: int, n: int) -> dict[str, Any] | None:
    """A rate with its likely range, in plain words. None when there is nothing to rate."""
    if n <= 0:
        return None
    low, high = wilson(k, n)
    if k == n:
        text = f"likely {_pct(low)} or more"
    elif k == 0:
        text = f"likely {_pct(high, True)} or less"
    else:
        text = f"likely {_pct(low)} to {_pct(high, True)}"
    return {"k": k, "n": n, "rate": round(k / n, 4), "low": round(low, 4), "high": round(high, 4),
            "text": text + f" (from {n})"}


def failures_text(failed: int, n: int) -> str | None:
    """What a count of failures says about the real failure rate. "0 of 30" means likely under 12%
    (Wilson; the simpler rule of three, about 3/n, gives 10% and is a little too sure for small n)."""
    if n <= 0:
        return None
    low, high = wilson(n - failed, n)
    worst = 1 - low
    if failed == 0:
        return f"No failure in {n}: the real failure rate is likely under {max(1, math.ceil(100 * worst))}%."
    best = 1 - high
    return f"{failed} of {n} failed: the real failure rate is likely {_pct(best)} to {_pct(worst, True)}."


def enough(p: float, n: int) -> bool:
    """Whether n values are enough for the p-th percentile to decide anything."""
    need = MIN_FOR.get(int(p))
    if need is None:
        need = math.ceil(1 / max(1e-9, 1 - p / 100))
    return n >= need


def need_for(p: float) -> int:
    return MIN_FOR.get(int(p)) or math.ceil(1 / max(1e-9, 1 - p / 100))


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided p-value of Fisher's exact test on the table [[a, b], [c, d]] (passed, failed per group).
    Small values (under 0.05) mean the two pass rates are really different, not chance."""
    n = a + b + c + d
    row1, col1 = a + b, a + c

    def prob(x: int) -> float:
        return math.comb(col1, x) * math.comb(n - col1, row1 - x) / math.comb(n, row1)

    lo, hi = max(0, row1 + col1 - n), min(row1, col1)
    observed = prob(a)
    total = sum(p for x in range(lo, hi + 1) if (p := prob(x)) <= observed * (1 + 1e-9))
    return min(1.0, total)


def percentile_range(values: list[int], p: float, conf: float = 0.95) -> tuple[int | None, int | None]:
    """The likely range of the true p-th percentile from these values (distribution-free: order statistics
    with binomial ranks). (low, high) are measured values; high is None when even the slowest measured
    value may be below the true percentile (too few values: "at least low"), and low is None the other way.

    Of 10 answers, the true p95 is at least the 8th slowest-from-the-bottom value and may be slower than
    anything measured: (x8, None). Of 60, both ends are measured values."""
    xs = sorted(values)
    n = len(xs)
    if not n:
        return None, None
    q, tail = p / 100, (1 - conf) / 2

    def cdf(k: int) -> float:                     # P(at most k of the n values are below the percentile)
        return sum(math.comb(n, i) * q ** i * (1 - q) ** (n - i) for i in range(0, k + 1)) if k >= 0 else 0.0

    low = max((r for r in range(1, n + 1) if cdf(r - 1) <= tail), default=None)
    high = min((r for r in range(1, n + 1) if 1 - cdf(r - 1) <= tail), default=None)
    return (xs[low - 1] if low else None), (xs[high - 1] if high else None)
