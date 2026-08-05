import json

from app.db import init_db
from app.main import SimilaritySearchInput, similarity_search
from app.marketplaces import MarketListing
from app.similarity import augment_basalam_listings, import_basalam_dataset, search_similar_products


def _write_product(path, product_id, name, price, category_id=248, category_title="مانتو و تونیک"):
    payload = {
        "product_id": product_id,
        "name": name,
        "category_id": category_id,
        "category_title": category_title,
        "price_toman": price,
        "stock": 3,
        "sales_count": 2,
        "rating_average": 4.5,
        "vendor_id": 10,
        "vendor_name": "غرفه تست",
        "tags": "زنانه | پوشاک",
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def test_import_and_search_basalam_similarity_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.settings.database_path", str(tmp_path / "test.db"))
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    file_path = dataset / "basalam_women_menteau_tunic_exact_category_248.jsonl"
    _write_product(file_path, 1, "مانتو کتی زنانه مشکی", 780_000)
    _write_product(file_path, 2, "تونیک زنانه نخی تابستانه", 390_000)
    _write_product(file_path, 3, "کیف دستی زنانه", 650_000, 244, "کیف زنانه")

    result = import_basalam_dataset(dataset)
    listings = search_similar_products(
        "مانتو زنانه مشکی",
        category_id=248,
        current_price=800_000,
        limit=5,
    )

    assert result.files == 1
    assert result.products == 3
    assert listings
    assert listings[0].external_id == "1"
    assert listings[0].price == 780_000


def test_similarity_search_endpoint_reads_imported_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.settings.database_path", str(tmp_path / "api.db"))
    init_db()
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    file_path = dataset / "basalam_women_bag_exact_category_244.jsonl"
    _write_product(file_path, 11, "کیف زنانه چرمی دوشی", 920_000, 244, "کیف زنانه")
    import_basalam_dataset(dataset)

    body = similarity_search(
        SimilaritySearchInput(query="کیف چرمی زنانه", category_id=244, limit=3)
    )
    assert body["count"] == 1
    assert body["listings"][0]["url"] == "https://basalam.com/p/11"


def test_dataset_augmentation_keeps_live_basalam_and_adds_indexed_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.settings.database_path", str(tmp_path / "merge.db"))
    init_db()
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _write_product(
        dataset / "basalam_women_bag_exact_category_244.jsonl",
        21,
        "کیف زنانه چرمی دوشی",
        920_000,
        244,
        "کیف زنانه",
    )
    _write_product(
        dataset / "basalam_women_bag_exact_category_244.jsonl",
        22,
        "کیف زنانه مجلسی چرمی",
        1_100_000,
        244,
        "کیف زنانه",
    )
    import_basalam_dataset(dataset)

    live = [
        MarketListing(
            "basalam",
            "کیف زنانه چرمی دوشی",
            950_000,
            "https://basalam.com/p/live",
            similarity=1,
            external_id="live",
        )
    ]
    merged, added = augment_basalam_listings(live, "کیف چرمی زنانه", category_id=244)

    assert added == 1
    assert len(merged) == 2
    assert merged[0].origin == "live"
