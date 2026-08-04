import asyncio

from app.llm import optimize_marketplace_queries


def test_marketplace_query_optimizer_parses_bilingual_json(monkeypatch):
    async def fake_call(_prompt, max_tokens=0):
        assert max_tokens == 350
        return """```json
        {
          "iran": "پالت رژگونه 6 رنگ مات",
          "trendyol": "6 renk mat allık paleti",
          "noon": "6 color matte blush palette"
        }
        ```"""

    monkeypatch.setattr("app.llm._call_llm", fake_call)
    result = asyncio.run(
        optimize_marketplace_queries(
            "Floral Flush Blush Palette 6 Color Pressed Powder Blush Palette"
        )
    )
    assert result == {
        "iran": "پالت رژگونه 6 رنگ مات",
        "trendyol": "6 renk mat allık paleti",
        "noon": "6 color matte blush palette",
    }


def test_marketplace_query_optimizer_rejects_incomplete_response(monkeypatch):
    async def fake_call(_prompt, max_tokens=0):
        return '{"iran":"پالت رژگونه"}'

    monkeypatch.setattr("app.llm._call_llm", fake_call)
    assert asyncio.run(optimize_marketplace_queries("blush palette")) == {}
