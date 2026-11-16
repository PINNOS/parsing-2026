"""
UPSERT-загрузка в SQLite с пакетным executemany и обновлением updated_at только при изменении.
"""

import sqlite3
from pathlib import Path
import pandas as pd
from datetime import datetime, timezone

DB = Path(__file__).resolve().parent.parent / "db" / "warehouse.db"
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

UPSERT_SQL = """
INSERT INTO products (product_key,title,price_cents,stock,rating,category,in_stock,source,source_url,parser_version,collected_at,updated_at)
VALUES (:product_key,:title,:price_cents,:stock,:rating,:category,:in_stock,:source,:source_url,:parser_version,:collected_at,:updated_at)
ON CONFLICT(product_key) DO UPDATE SET
  title=excluded.title,
  price_cents=excluded.price_cents,
  stock=excluded.stock,
  rating=excluded.rating,
  category=excluded.category,
  in_stock=excluded.in_stock,
  source_url=excluded.source_url,
  parser_version=excluded.parser_version,
  collected_at=excluded.collected_at,
  updated_at=excluded.updated_at
WHERE products.price_cents IS NOT excluded.price_cents
   OR products.stock IS NOT excluded.stock
   OR products.title IS NOT excluded.title
   OR products.category IS NOT excluded.category
   OR products.rating IS NOT excluded.rating
"""


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_cents(v) -> int:
    """Деньги храним целым числом минорных единиц (копеек) — без float."""
    return int(round(float(v) * 100))


def load_frame(df: pd.DataFrame):
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA.read_text(encoding="utf-8") if SCHEMA.exists() else "")
    payload = []
    for _, r in df.iterrows():
        payload.append(
            {
                "product_key": r["product_key"],
                "title": r["title"],
                "price_cents": to_cents(r["price"]),
                "stock": None if pd.isna(r.get("stock")) else int(r["stock"]),
                "rating": None if pd.isna(r.get("rating")) else int(r["rating"]),
                "category": r.get("category"),
                "in_stock": int(bool(r["in_stock"])),
                "source": r["source"],
                "source_url": r["source_url"],
                "parser_version": r["parser_version"],
                "collected_at": str(r["collected_at"]),
                "updated_at": now_utc(),
            }
        )
    cur = conn.executemany(UPSERT_SQL, payload)
    conn.commit()
    conn.close()
    return {"loaded": len(payload), "rowcount_effective": cur.rowcount}


if __name__ == "__main__":
    print("load.py ready")
