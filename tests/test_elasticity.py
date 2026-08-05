from app.elasticity import ElasticityPoint, analyze_elasticity


def test_elasticity_reports_demand_and_revenue_scenarios():
    points = [
        ElasticityPoint(100_000, 100, "1"),
        ElasticityPoint(110_000, 90, "2"),
        ElasticityPoint(120_000, 80, "3"),
        ElasticityPoint(130_000, 70, "4"),
    ]
    result = analyze_elasticity(points, current_price=110_000)
    assert result["status"] == "ready"
    assert result["elasticity"] < 0
    assert result["scenarios"]
    assert all("revenue_change_percent" in item for item in result["scenarios"])


def test_elasticity_requires_price_variation():
    result = analyze_elasticity(
        [ElasticityPoint(100_000, 10, "1"), ElasticityPoint(100_000, 12, "2")],
        current_price=100_000,
    )
    assert result["status"] == "insufficient_data"
