from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings
from app.marketplaces import MarketListing


async def score_product_similarity(
    query: str,
    listings: list[MarketListing],
) -> dict[str, float]:
    """Score each listing's similarity to the query using an LLM.

    Returns a mapping of external_id -> similarity score (0.0–1.0).
    Returns an empty dict if LLM similarity is disabled or the API fails.
    """
    if not settings.llm_similarity_enabled or not settings.avalai_api_key:
        return {}

    if not listings:
        return {}

    products_text = "\n".join(
        f"{i + 1}. {listing.title}" for i, listing in enumerate(listings)
    )

    prompt = f'''You are a product matching assistant. A shopper is searching for: "{query}"

Rate how well each product matches the search query on a scale of 0-100.
0 = completely unrelated, 100 = exact match. Consider product type, brand, model, and key specifications.

Products:
{products_text}

Return ONLY a JSON array of integers (0-100), one per product, in the same order.
Example: [85, 42, 90, 7, 100]'''

    try:
        async with httpx.AsyncClient(
            base_url=settings.avalai_base_url,
            timeout=httpx.Timeout(30, connect=10),
            headers={
                "Authorization": f"Bearer {settings.avalai_api_key}",
                "Content-Type": "application/json",
            },
        ) as client:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": settings.avalai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max(4000, len(listings) * 4),
                    "temperature": 0.1,
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception:
        return {}

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    try:
        scores = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}

    if not isinstance(scores, list):
        return {}

    result: dict[str, float] = {}
    for i, listing in enumerate(listings):
        if i < len(scores) and listing.external_id:
            try:
                score = float(scores[i])
                result[listing.external_id] = max(0.0, min(100.0, score)) / 100.0
            except (ValueError, TypeError):
                continue
    return result
