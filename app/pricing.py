from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from statistics import median


@dataclass(frozen=True)
class PriceBand:
    low: int
    suggested: int
    high: int
    confidence: int
    sample_size: int
    reason: str


@dataclass(frozen=True)
class RepriceDecision:
    should_change: bool
    new_price: int
    reason: str


def _percentile(sorted_values: list[int], ratio: float) -> float:
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = ratio * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _round_price(value: float) -> int:
    """Round Iranian-toman prices to a useful, conservative display step."""
    step = 1_000 if value < 1_000_000 else 10_000
    return max(step, int(round(value / step) * step))


def recommend_price(
    current_price: int,
    comparable_prices: list[int],
    *,
    views: int = 0,
    sales: int = 0,
    objective: str = "balanced",
) -> PriceBand:
    valid = sorted(price for price in comparable_prices if price > 0)
    if not valid:
        return PriceBand(
            low=_round_price(current_price * 0.90),
            suggested=current_price,
            high=_round_price(current_price * 1.10),
            confidence=15,
            sample_size=0,
            reason="داده مشابه کافی نیست؛ قیمت فعلی حفظ شد.",
        )

    # Remove extreme marketplace prices using a robust IQR fence.
    if len(valid) >= 4:
        q1 = _percentile(valid, 0.25)
        q3 = _percentile(valid, 0.75)
        iqr = q3 - q1
        filtered = [p for p in valid if q1 - 1.5 * iqr <= p <= q3 + 1.5 * iqr]
        valid = filtered or valid

    low = _percentile(valid, 0.25)
    center = float(median(valid))
    high = _percentile(valid, 0.75)
    target_ratio = {"fast": 0.35, "balanced": 0.50, "margin": 0.65}.get(objective, 0.50)
    suggested = _percentile(valid, target_ratio)

    # Many views with no sale is a weak signal that a lower point in the band is useful.
    if views >= 100 and sales == 0:
        suggested = min(suggested, _percentile(valid, 0.40))
        signal = "بازدید بالا و فروش صفر، پیشنهاد را کمی رقابتی‌تر کرد."
    elif sales >= 3:
        suggested = max(suggested, center)
        signal = "سابقه فروش خوب اجازه حفظ حاشیه بیشتر را می‌دهد."
    else:
        signal = "پیشنهاد از میانه قیمت محصولات مشابه ساخته شد."

    confidence = min(95, 25 + len(valid) * 5)
    return PriceBand(
        low=_round_price(low),
        suggested=_round_price(suggested),
        high=_round_price(high),
        confidence=confidence,
        sample_size=len(valid),
        reason=signal,
    )


def decide_reprice(
    *,
    current_price: int,
    suggested_price: int,
    floor_price: int,
    days_since_change: int,
    interval_days: int,
    max_drop_percent: float,
    sold: bool = False,
) -> RepriceDecision:
    if sold:
        return RepriceDecision(False, current_price, "محصول فروخته شده است.")
    if days_since_change < interval_days:
        return RepriceDecision(False, current_price, "هنوز زمان بازبینی بعدی نرسیده است.")
    if current_price <= floor_price:
        return RepriceDecision(False, current_price, "قیمت به کف تعیین‌شده رسیده است.")

    max_drop = ceil(current_price * max_drop_percent / 100)
    guarded_target = max(floor_price, current_price - max_drop, suggested_price)
    guarded_target = _round_price(guarded_target)
    if guarded_target >= current_price:
        return RepriceDecision(False, current_price, "کاهش قیمت مزیت معناداری ندارد.")
    return RepriceDecision(
        True,
        guarded_target,
        f"کاهش کنترل‌شده؛ حداکثر {max_drop_percent:g}٪ و هرگز پایین‌تر از کف.",
    )

