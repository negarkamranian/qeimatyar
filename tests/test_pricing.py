from app.pricing import decide_reprice, recommend_price


def test_recommendation_ignores_extreme_outlier():
    band = recommend_price(
        500_000,
        [420_000, 450_000, 470_000, 490_000, 510_000, 2_900_000],
    )
    assert band.high < 1_000_000
    assert band.sample_size == 5
    assert band.low <= band.suggested <= band.high


def test_no_comparables_keeps_current_price():
    band = recommend_price(250_000, [])
    assert band.suggested == 250_000
    assert band.confidence == 15


def test_reprice_never_crosses_floor_or_max_drop():
    decision = decide_reprice(
        current_price=500_000,
        suggested_price=300_000,
        floor_price=410_000,
        days_since_change=4,
        interval_days=3,
        max_drop_percent=5,
    )
    assert decision.should_change
    assert decision.new_price == 475_000


def test_reprice_waits_for_interval():
    decision = decide_reprice(
        current_price=500_000,
        suggested_price=450_000,
        floor_price=400_000,
        days_since_change=1,
        interval_days=3,
        max_drop_percent=5,
    )
    assert not decision.should_change

