from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.config import settings
from app.db import connection, init_db, now_iso
from app.marketplaces import MarketListing, normalize_text, title_similarity


WOMEN_APPAREL_CATEGORY_IDS = frozenset(range(228, 249))


@dataclass(frozen=True)
class DatasetImportResult:
    files: int
    products: int


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dataset_files(dataset_dir: str | Path) -> list[Path]:
    root = Path(dataset_dir)
    return sorted(root.glob("basalam_women_*_exact_category_*.jsonl"))


def _iter_products(paths: Iterable[Path]) -> Iterable[tuple[Path, dict[str, Any]]]:
    for path in paths:
        with path.open(encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield path, payload


def import_basalam_dataset(dataset_dir: str | Path | None = None) -> DatasetImportResult:
    """Load light Basalam women apparel JSONL files into SQLite and FTS.

    The importer is idempotent for the current snapshot: it replaces catalog rows
    from the dataset and rebuilds the FTS table. Keep images/videos outside this
    path; this catalog is intentionally text/numeric only.
    """
    paths = _dataset_files(dataset_dir or settings.basalam_dataset_dir)
    imported_at = now_iso()
    products = 0

    init_db()
    with connection() as db:
        db.execute("DELETE FROM basalam_product_catalog")
        db.execute("DELETE FROM basalam_product_catalog_fts")
        for path, item in _iter_products(paths):
            product_id = _safe_int(item.get("product_id"))
            title = " ".join(str(item.get("name") or item.get("title") or "").split())
            price = _safe_int(item.get("price_toman"))
            if not product_id or not title or not price or price <= 0:
                continue
            category_id = _safe_int(item.get("category_id"))
            category_title = item.get("category_title") or ""
            tags = item.get("tags") or ""
            db.execute(
                """INSERT OR REPLACE INTO basalam_product_catalog
                (product_id,title,category_id,category_title,price_toman,stock,
                 sales_count,rating_average,vendor_id,vendor_name,indexed_at,source_file,imported_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    product_id,
                    title,
                    category_id,
                    category_title,
                    price,
                    _safe_int(item.get("stock")),
                    _safe_int(item.get("sales_count")),
                    _safe_float(item.get("rating_average")),
                    _safe_int(item.get("vendor_id")),
                    item.get("vendor_name") or "",
                    item.get("indexed_at") or "",
                    path.name,
                    imported_at,
                ),
            )
            db.execute(
                """INSERT INTO basalam_product_catalog_fts
                (rowid,title,category_title,tags,product_id)
                VALUES (?,?,?,?,?)""",
                (product_id, title, category_title, tags, product_id),
            )
            products += 1
    return DatasetImportResult(files=len(paths), products=products)


def _fts_query(value: str) -> str:
    tokens = [
        token
        for token in normalize_text(value).split()
        if len(token) > 1 and not token.isdigit()
    ]
    if not tokens:
        return ""
    return " OR ".join(f"{token}*" for token in tokens[:10])


def search_similar_products(
    query: str,
    *,
    category_id: int | None = None,
    current_price: int | None = None,
    limit: int = 36,
    min_similarity: float = 0.12,
) -> list[MarketListing]:
    """Return similar Basalam dataset products as MarketListing objects."""
    query = " ".join(query.split()).strip()
    if len(query) < 2:
        return []
    fts_query = _fts_query(query)
    if not fts_query:
        return []

    sql = """
        SELECT c.product_id,c.title,c.category_id,c.category_title,c.price_toman,
               c.stock,c.sales_count,c.rating_average
        FROM basalam_product_catalog_fts f
        JOIN basalam_product_catalog c ON c.product_id=f.product_id
        WHERE basalam_product_catalog_fts MATCH ?
          AND c.price_toman > 0
    """
    params: list[Any] = [fts_query]
    if category_id:
        sql += " AND c.category_id=?"
        params.append(category_id)
    sql += " LIMIT ?"
    params.append(max(limit * 8, 80))

    try:
        with connection() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []

    listings: list[MarketListing] = []
    for row in rows:
        similarity = title_similarity(query, row["title"])
        if category_id and row.get("category_id") == category_id:
            similarity = min(1.0, similarity + 0.12)
        if current_price and current_price > 0:
            ratio = row["price_toman"] / current_price
            if ratio < 0.25 or ratio > 4:
                similarity *= 0.55
        if similarity < min_similarity:
            continue
        listings.append(
            MarketListing(
                source="basalam",
                title=row["title"],
                price=int(row["price_toman"]),
                url=f"https://basalam.com/p/{row['product_id']}",
                similarity=similarity,
                external_id=str(row["product_id"]),
                origin="dataset",
            )
        )
    listings.sort(key=lambda item: (-item.similarity, item.price))
    return listings[:limit]


def augment_basalam_listings(
    listings: list[MarketListing],
    query: str,
    *,
    category_id: int | None = None,
    current_price: int | None = None,
    limit: int = 72,
) -> tuple[list[MarketListing], int]:
    """Add indexed Basalam comparables and remove duplicate live results.

    Live results win when the same product is present in both sources. The
    returned count is the number of dataset rows that were actually added.
    """
    local = search_similar_products(
        query,
        category_id=category_id,
        current_price=current_price,
        limit=limit,
    )
    if not local:
        return listings, 0

    result: list[MarketListing] = []
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for item in listings:
        if item.source == "basalam":
            if item.external_id:
                seen_ids.add(item.external_id)
            seen_titles.add(normalize_text(item.title))
        result.append(item)

    added = 0
    for item in local:
        title_key = normalize_text(item.title)
        if (item.external_id and item.external_id in seen_ids) or title_key in seen_titles:
            continue
        result.append(item)
        seen_ids.add(item.external_id)
        seen_titles.add(title_key)
        added += 1

    result.sort(key=lambda item: (-item.similarity, item.price))
    return result[:limit], added


def main() -> None:
    parser = argparse.ArgumentParser(description="Import/search the local Basalam women apparel dataset.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--dataset-dir", default=settings.basalam_dataset_dir)
    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--category-id", type=int)
    search_parser.add_argument("--current-price", type=int)
    search_parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if args.command == "import":
        result = import_basalam_dataset(args.dataset_dir)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    elif args.command == "search":
        listings = search_similar_products(
            args.query,
            category_id=args.category_id,
            current_price=args.current_price,
            limit=args.limit,
        )
        print(json.dumps([item.public_dict() for item in listings], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
