from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                user_id INTEGER PRIMARY KEY,
                vendor_id INTEGER NOT NULL,
                vendor_title TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                connected_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                vendor_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                views INTEGER NOT NULL DEFAULT 0,
                sales INTEGER NOT NULL DEFAULT 0,
                image_url TEXT,
                comparable_prices TEXT NOT NULL DEFAULT '[]',
                synced_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS policies (
                product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0,
                floor_price INTEGER NOT NULL,
                objective TEXT NOT NULL DEFAULT 'balanced',
                interval_days INTEGER NOT NULL DEFAULT 3,
                max_drop_percent REAL NOT NULL DEFAULT 5,
                last_changed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                old_price INTEGER NOT NULL,
                new_price INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                customer_id INTEGER PRIMARY KEY,
                subscription_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                period_end TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS merchant_products (
                user_id INTEGER NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                current_price INTEGER NOT NULL DEFAULT 0,
                stock INTEGER NOT NULL DEFAULT 0,
                image_url TEXT,
                market_low INTEGER,
                market_suggested INTEGER,
                market_high INTEGER,
                user_min INTEGER,
                user_max INTEGER,
                confidence INTEGER,
                sample_size INTEGER,
                source_counts TEXT NOT NULL DEFAULT '{}',
                estimate_error TEXT,
                synced_at TEXT NOT NULL,
                estimated_at TEXT,
                PRIMARY KEY (user_id, product_id)
            );
            CREATE INDEX IF NOT EXISTS idx_merchant_products_user
              ON merchant_products(user_id);
            CREATE TABLE IF NOT EXISTS merchant_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                target_url TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                read_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_merchant_notifications_user_created
              ON merchant_notifications(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_merchant_notifications_user_unread
              ON merchant_notifications(user_id, read_at);
            CREATE TABLE IF NOT EXISTS currency_rate_state (
                symbol TEXT PRIMARY KEY,
                last_price_toman INTEGER NOT NULL,
                last_notified_price_toman INTEGER,
                last_checked_at TEXT NOT NULL,
                last_notified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT,
                user_id INTEGER,
                feedback_type TEXT NOT NULL,
                target_url TEXT NOT NULL,
                rating INTEGER NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_feedback_target
              ON user_feedback(target_url, created_at);
            CREATE TABLE IF NOT EXISTS button_click_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                button_name TEXT NOT NULL,
                product_id TEXT,
                store_id TEXT,
                product_url TEXT,
                client_id TEXT,
                user_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_button_clicks_button
              ON button_click_metrics(button_name, created_at);
            CREATE TABLE IF NOT EXISTS store_page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id TEXT NOT NULL,
                client_id TEXT,
                user_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_store_page_views_store
              ON store_page_views(store_id, created_at);
            CREATE TABLE IF NOT EXISTS search_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT,
                user_id INTEGER,
                query TEXT NOT NULL,
                resolved_from_url INTEGER NOT NULL DEFAULT 0,
                source_product_id TEXT,
                result_count INTEGER NOT NULL,
                used_llm INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_search_analytics_created
              ON search_analytics(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_search_analytics_query
              ON search_analytics(query);
            """
        )
        account_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(accounts)").fetchall()
        }
        migrations = {
            "user_name": "ALTER TABLE accounts ADD COLUMN user_name TEXT",
            "token_expires_at": "ALTER TABLE accounts ADD COLUMN token_expires_at TEXT",
            "last_synced_at": "ALTER TABLE accounts ADD COLUMN last_synced_at TEXT",
            "sync_status": "ALTER TABLE accounts ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'idle'",
            "sync_error": "ALTER TABLE accounts ADD COLUMN sync_error TEXT",
        }
        for column, statement in migrations.items():
            if column not in account_columns:
                db.execute(statement)

        button_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(button_click_metrics)").fetchall()
        }
        if "product_url" not in button_columns:
            db.execute("ALTER TABLE button_click_metrics ADD COLUMN product_url TEXT")


def seed_demo() -> None:
    with connection() as db:
        count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count:
            return
        products = [
            (10771511, 855658, "عسل طبیعی آویشن کوهی ۹۰۰ گرمی", 489000, 18, 147, 2, "", [425000, 449000, 470000, 485000, 510000, 535000, 890000]),
            (24018670, 855658, "دمنوش زعفران و گل محمدی", 218000, 31, 86, 0, "", [175000, 189000, 205000, 215000, 225000, 245000]),
            (24018671, 855658, "شیره انگور سنتی یک کیلویی", 335000, 9, 42, 4, "", [290000, 310000, 325000, 340000, 370000, 390000]),
            (24018672, 855658, "حلوا ارده کنجدی ۵۰۰ گرمی", 195000, 0, 54, 1, "", [169000, 180000, 185000, 199000, 210000]),
        ]
        for row in products:
            db.execute(
                """INSERT INTO products
                (id,vendor_id,title,price,stock,views,sales,image_url,comparable_prices,synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (*row[:-1], json.dumps(row[-1]), now_iso()),
            )
            db.execute(
                """INSERT INTO policies
                (product_id,enabled,floor_price,objective,interval_days,max_drop_percent,last_changed_at)
                VALUES (?,?,?,?,?,?,?)""",
                (row[0], 1 if row[0] != 24018672 else 0, int(row[3] * 0.82), "balanced", 3, 5, now_iso()),
            )


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connection() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]
