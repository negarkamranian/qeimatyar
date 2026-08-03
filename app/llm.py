from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.marketplaces import MarketListing

logger = logging.getLogger(__name__)


async def optimize_search_query(source_product: dict[str, Any]) -> str:
    """Use LLM to build the best search query from product details.

    Extracts brand, model, key specs, and important keywords to form
    the most precise search string for marketplace crawling.
    """
    context = _build_source_context(source_product)
    prompt = f'''You are an e-commerce search query expert.

Given the following product details, produce the single best search query string for finding this exact product on Persian marketplaces (Torob, Digikala, Basalam).

Rules:
- Include brand, model, key specs (capacity, color, size, weight, etc.)
- Do NOT include prices, vendor names, or non-essential details
- Keep it concise (under 100 characters)
- Output ONLY the query text, no formatting

Product details:
{context}
'''

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
                    "max_tokens": 200,
                    "temperature": 0.1,
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("LLM query optimization failed: %s", exc)
        return ""

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    if content and len(content) <= 200:
        logger.info("LLM query optimized: %s -> %s", source_product.get("title", "")[:50], content)
        return content
    return ""


def _build_source_context(source_product: dict[str, Any]) -> str:
    """Build a human-readable description of the source product for the LLM prompt."""
    parts: list[str] = []
    parts.append(f"Title: {source_product.get('title', '')}")
    if brand := source_product.get("brand"):
        if brand := str(brand).strip():
            parts.append(f"Brand: {brand}")
    if desc := source_product.get("description"):
        if desc := str(desc).strip():
            parts.append(f"Description: {desc[:500]}")
    specs: dict[str, str] = source_product.get("specs", {}) or {}
    if specs:
        specs_text = "; ".join(f"{k}: {v}" for k, v in specs.items() if v)
        if specs_text:
            parts.append(f"Specifications: {specs_text[:500]}")
    return "\n".join(parts)


async def score_product_similarity(
    query: str,
    listings: list[MarketListing],
    source_product: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Score each listing's similarity to the search query or source product using an LLM.

    Returns a mapping of listing URL -> similarity score (0.0–1.0).
    Returns an empty dict if LLM similarity is disabled or the API fails.
    """
    if not settings.llm_similarity_enabled or not settings.avalai_api_key:
        return {}

    if not listings:
        return {}

    products_text = "\n".join(
        f"{i + 1}. {listing.title}" for i, listing in enumerate(listings)
    )

    if source_product:
        context = _build_source_context(source_product)
        prompt = _build_product_comparison_prompt(query, context, products_text)
    else:
        prompt = _build_query_comparison_prompt(query, products_text)

    try:
        async with httpx.AsyncClient(
            base_url=settings.avalai_base_url,
            timeout=httpx.Timeout(60, connect=15),
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
                    "max_tokens": max(8000, len(listings) * 8),
                    "temperature": 0.1,
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("LLM similarity scoring failed: %s", exc)
        return {}

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    try:
        scores = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        logger.warning("LLM response was not valid JSON: %s", content[:200])
        return {}

    if not isinstance(scores, list):
        logger.warning("LLM response was not a list")
        return {}

    result: dict[str, float] = {}
    matched = 0
    for i, listing in enumerate(listings):
        if i < len(scores) and listing.url:
            try:
                score = float(scores[i])
                score = max(0.0, min(100.0, score)) / 100.0
                result[listing.url] = score
                matched += 1
            except (ValueError, TypeError):
                continue
    logger.info(
        "LLM similarity scoring complete: %d/%d listings scored (query: %s)",
        matched,
        len(listings),
        query,
    )
    return result


def _build_query_comparison_prompt(query: str, products_text: str) -> str:
    """Build a prompt comparing marketplace products to a text search query."""
    return f'''You are an e-commerce product matching expert.

A shopper is searching for: "{query}"

For each product, assign a similarity score from 0-100 based on how well it matches the search intent.

SCORING CRITERIA (evaluate each dimension, then combine into a final 0-100 score):
1. PRODUCT TYPE MATCH (0-40 points): Does this product belong to the same general category?
   - Same exact category (e.g. both phones) = 35-40
   - Related category (e.g. phone case when searching for phone) = 15-30
   - Different category (e.g. phone when searching for TV) = 0-5
2. BRAND MATCH (0-25 points): Is the brand the same or compatible?
   - Same brand = 20-25
   - Compatible/generic = 10-15
   - Wrong brand = 0-5
3. MODEL/SPECIFICITY MATCH (0-25 points): Does the model or key specs align?
   - Exact model match = 20-25
   - Close variant (e.g. different color/capacity) = 10-18
   - Generic or unrelated model = 0-5
4. TITLE KEYWORD OVERLAP (0-10 points): How many meaningful search keywords appear in the title?
   - All keywords = 8-10
   - Most keywords = 5-7
   - Few/no keywords = 0-2

FINAL SCORE = product_type + brand + model + keyword_overlap, capped at 100.

EXAMPLES (query: "آیفون 13 پرو 128"):
- "آیفون 13 پرو 128GB" → 100 (exact match, all criteria maxed)
- "آیفون 13 پرو 256GB" → 88 (same model, different capacity)
- "آیفون 13 پرو" → 85 (same model, no capacity mentioned)
- "آیفون 12 پرو 128" → 68 (previous generation)
- "کاور آیفون 13 پرو" → 25 (case, not the phone itself)
- "ساعت هوشمند Apple" → 5 (completely different product)

Products:
{products_text}

Return ONLY a JSON array of integers (0-100), one per product, in the same order.
Example: [85, 42, 90, 7, 100]'''


def _build_product_comparison_prompt(
    query: str, source_context: str, products_text: str
) -> str:
    """Build a prompt comparing marketplace products to a full source product."""
    return f'''You are an expert e-commerce product matching assistant.

A shopper is viewing this product on Basalam:
{source_context}

Now you need to find the most similar products from these marketplace search results.
Rate each result product from 0-100 based on how well it matches the source product above.

SCORING CRITERIA (evaluate each dimension, then combine into a final 0-100 score):
1. PRODUCT TYPE MATCH (0-40 points): Is it the same type of product?
   - Same product type (e.g. source is phone, result is phone) = 35-40
   - Related/Accessory category (e.g. phone case, screen protector) = 15-30
   - Completely different product = 0-5
2. BRAND MATCH (0-25 points): Is the brand the same?
   - Same brand = 20-25
   - Compatible/generic/unbranded = 10-15
   - Different brand = 0-5
3. MODEL/SPECIFICITY MATCH (0-25 points): Does the model or key specs match?
   - Exact model + specs = 20-25
   - Same model with minor spec differences = 10-18
   - Generic or unrelated model = 0-5
4. TITLE KEYWORD OVERLAP (0-10 points): How many key terms from the source appear in the result title?
   - All key terms = 8-10
   - Most key terms = 5-7
   - Few/no key terms = 0-2

FINAL SCORE = product_type + brand + model + keyword_overlap, capped at 100.

EXAMPLES (source: "آیفون 13 پرو 128GB"):
- "آیفون 13 پرو 256GB" → 88 (exact model, different capacity)
- "آیفون 13 پرو" → 85 (same model, capacity not specified)
- "آیفون 12 پرو 128GB" → 68 (previous generation model)
- "کاور آیفون 13 پرو" → 25 (accessory, not the actual phone)
- "ساعت هوشمند شیائومی" → 5 (completely different product)
- "آیفون 14 پرو 128GB" → 60 (newer generation, same line)

Marketplace search results (24 per source, up to 72 total):
{products_text}

Return ONLY a JSON array of integers (0-100), one per product, in the same order.
Example: [88, 85, 68, 25, 5, 60]'''
