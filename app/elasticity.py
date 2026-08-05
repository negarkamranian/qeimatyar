from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ElasticityPoint:
    price: float
    units: float
    period: str
    source: str = "manual"


def _regression(points: list[ElasticityPoint]) -> tuple[float, float, float] | None:
    usable = [point for point in points if point.price > 0 and point.units > 0]
    if len(usable) < 3 or len({point.price for point in usable}) < 2:
        return None
    xs = [math.log(point.price) for point in usable]
    ys = [math.log(point.units) for point in usable]
    x_bar = sum(xs) / len(xs)
    y_bar = sum(ys) / len(ys)
    denominator = sum((x - x_bar) ** 2 for x in xs)
    if denominator <= 1e-12:
        return None
    slope = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denominator
    intercept = y_bar - slope * x_bar
    predicted = [intercept + slope * x for x in xs]
    ss_total = sum((y - y_bar) ** 2 for y in ys)
    r_squared = 1.0 - sum((y - p) ** 2 for y, p in zip(ys, predicted)) / ss_total if ss_total else 0.0
    return slope, intercept, max(0.0, min(1.0, r_squared))


def analyze_elasticity(
    points: list[ElasticityPoint],
    *,
    current_price: int,
    market_suggested: int | None = None,
) -> dict[str, Any]:
    usable = [point for point in points if point.price > 0 and point.units >= 0]
    positive = [point for point in usable if point.units > 0]
    regression = _regression(positive)
    baseline_price = float(current_price or (market_suggested or 0))
    if not regression or baseline_price <= 0:
        return {
            "status": "insufficient_data",
            "elasticity": None,
            "confidence": 0,
            "sample_size": len(usable),
            "distinct_prices": len({point.price for point in positive}),
            "points": [point.__dict__ for point in usable],
            "scenarios": [],
            "recommended_price": None,
            "message": "برای برآورد کشش، حداقل ۳ رکورد فروش در ۲ سطح قیمت متفاوت لازم است.",
        }

    elasticity, intercept, r_squared = regression
    observed_prices = [point.price for point in positive]
    low = min(observed_prices)
    high = max(observed_prices)
    rounding_step = 1000 if baseline_price >= 1000 else 1
    candidates = sorted({
        max(1, int(round(baseline_price * factor / rounding_step)) * rounding_step)
        for factor in (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15)
    })
    if market_suggested and market_suggested > 0:
        candidates.append(int(market_suggested))
    candidates = sorted(set(candidates))

    def demand(price: float) -> float:
        return math.exp(intercept) * price ** elasticity

    base_units = demand(baseline_price)
    base_revenue = baseline_price * base_units
    scenarios = []
    for price in candidates:
        predicted_units = demand(price)
        revenue = price * predicted_units
        scenarios.append({
            "price": price,
            "demand_units": round(predicted_units, 2),
            "demand_change_percent": round((predicted_units / base_units - 1) * 100, 2),
            "revenue": round(revenue),
            "revenue_change_percent": round((revenue / base_revenue - 1) * 100, 2),
            "within_observed_range": low <= price <= high,
        })
    best = max(scenarios, key=lambda item: item["revenue"])
    confidence = min(95, max(15, int(35 + min(30, len(positive) * 5) + r_squared * 30)))
    return {
        "status": "ready",
        "elasticity": round(elasticity, 4),
        "elasticity_percent_per_one_percent": round(elasticity, 4),
        "interpretation": "کشش‌پذیر؛ افزایش قیمت تقاضا را بیشتر کاهش می‌دهد." if elasticity < -1 else "کم‌کشش؛ افزایش قیمت در این داده اثر کمتری بر تقاضا دارد.",
        "confidence": confidence,
        "r_squared": round(r_squared, 4),
        "sample_size": len(usable),
        "positive_sales_points": len(positive),
        "distinct_prices": len({point.price for point in positive}),
        "observed_price_range": {"low": low, "high": high},
        "baseline_price": baseline_price,
        "points": [point.__dict__ for point in usable],
        "scenarios": scenarios,
        "recommended_price": best["price"],
        "recommended_revenue": best["revenue"],
        "message": "پیشنهاد در محدوده سناریوهای بررسی‌شده است؛ برای تصمیم قطعی، اثر فصل، تبلیغات و موجودی را هم کنترل کنید.",
    }


def estimate_log_slope(pairs: list[tuple[float, float]]) -> dict[str, Any] | None:
    usable = [(x, y) for x, y in pairs if x > 0 and y > 0]
    if len(usable) < 3 or len({x for x, _ in usable}) < 2:
        return None
    xs = [math.log(x) for x, _ in usable]
    ys = [math.log(y) for _, y in usable]
    x_bar = sum(xs) / len(xs)
    y_bar = sum(ys) / len(ys)
    denominator = sum((x - x_bar) ** 2 for x in xs)
    if denominator <= 1e-12:
        return None
    slope = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denominator
    predicted = [y_bar + slope * (x - x_bar) for x in xs]
    total = sum((y - y_bar) ** 2 for y in ys)
    r_squared = 1 - sum((y - p) ** 2 for y, p in zip(ys, predicted)) / total if total else 0
    return {"pass_through": round(slope, 4), "r_squared": round(max(0, min(1, r_squared)), 4), "sample_size": len(usable)}
