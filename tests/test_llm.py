import asyncio

from app.llm import AsyncTTLCache, optimize_marketplace_queries


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


def test_marketplace_query_optimizer_caches_successful_results(monkeypatch):
    calls = 0

    async def fake_call(_prompt, max_tokens=0):
        nonlocal calls
        calls += 1
        return (
            '{"iran":"فلاسک 1 لیتری","trendyol":"1 litre termos",'
            '"noon":"1 litre flask"}'
        )

    monkeypatch.setattr("app.llm._call_llm", fake_call)

    async def run() -> None:
        title = "unique cache test vacuum flask one litre"
        first, second = await asyncio.gather(
            optimize_marketplace_queries(title),
            optimize_marketplace_queries(title),
        )
        assert first == second

    asyncio.run(run())
    assert calls == 1


def test_async_ttl_cache_does_not_cache_unsuccessful_values():
    calls = 0

    async def compute() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {}

    async def run() -> None:
        cache = AsyncTTLCache(ttl_seconds=60)
        assert await cache.get_or_compute("key", compute, bool) == {}
        assert await cache.get_or_compute("key", compute, bool) == {}

    asyncio.run(run())
    assert calls == 2
