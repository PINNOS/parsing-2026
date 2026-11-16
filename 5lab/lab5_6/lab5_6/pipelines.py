"""Item Pipeline: четыре обязательных звена.

100 ValidationPipeline — обязательные поля, DropItem + счётчик.
200 CleanPipeline      — очистка/типизация, DropItem при нечисловом значении.
300 DedupPipeline      — дедупликация по бизнес-ключу (не по URL).
400 SQLitePipeline     — пакетная запись в общую БД с двумя таблицами.

Порядок задан в ITEM_PIPELINES (settings.py); статистика берётся из
crawler, соединение с БД открывается в open_spider и закрывается
в close_spider — в process_item его нет (требование задания).
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem

REQUIRED = {
    "books": [
        "upc",
        "title",
        "price_excl_tax",
        "price_incl_tax",
        "availability",
        "num_reviews",
        "category",
    ],
    "quotes": ["quote", "author"],
}
PRICE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")
QTY_RE = re.compile(r"\((\d+)\s+available\)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    upc            TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    list_price     REAL,
    rating         INTEGER,
    price_excl_tax REAL,
    price_incl_tax REAL,
    tax            REAL,
    availability   TEXT,
    quantity       INTEGER,
    num_reviews    INTEGER,
    category       TEXT,
    description    TEXT,
    source         TEXT,
    collected_at   TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    business_key   TEXT PRIMARY KEY,
    quote          TEXT NOT NULL,
    author         TEXT NOT NULL,
    author_url     TEXT,
    tags           TEXT,
    born_date      TEXT,
    born_date_raw  TEXT,
    born_location  TEXT,
    biography      TEXT,
    source         TEXT,
    collected_at   TEXT
);
"""


class ValidatePipeline:
    """Звено 100: проверка обязательных полей."""

    def __init__(self, stats):
        self.stats = stats

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.stats)

    def process_item(self, item):
        source = item.get("source")
        missing = [f for f in REQUIRED.get(source, []) if not item.get(f)]
        if missing:
            self.stats.inc_value("dropped/missing_fields")
            self.stats.inc_value(f"dropped/missing_fields/{source}")
            raise DropItem(f"нет обязательных полей {missing}")
        return item


class CleanPipeline:
    """Звено 200: очистка и типизация. Нечисловое значение -> DropItem."""

    def __init__(self, stats):
        self.stats = stats

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.stats)

    def process_item(self, item):
        ad = ItemAdapter(item)
        source = ad.get("source")
        try:
            if source == "books":
                for f in ("list_price", "price_excl_tax", "price_incl_tax", "tax"):
                    raw = ad.get(f)
                    if raw:
                        m = PRICE_RE.search(str(raw))
                        if not m:
                            raise ValueError(f"{f}={raw!r}")
                        ad[f] = float(m.group(1))
                ad["num_reviews"] = int(str(ad.get("num_reviews", "0")).strip())
                avail = str(ad.get("availability") or "")
                m = QTY_RE.search(avail)
                ad["quantity"] = int(m.group(1)) if m else 0
                ad["title"] = " ".join(str(ad.get("title")).split())
            else:  # quotes
                ad["quote"] = " ".join(str(ad.get("quote")).split())
                ad["author"] = " ".join(str(ad.get("author")).split()).title()
                # список тегов храним JSON-строкой — типизация под колонку TEXT
                if isinstance(ad.get("tags"), (list, tuple)):
                    ad["tags"] = json.dumps(list(ad["tags"]), ensure_ascii=False)
                raw_date = ad.get("born_date_raw")
                if raw_date:
                    # "September 19, 1948" -> "1948-09-19"
                    ad["born_date"] = (
                        datetime.strptime(raw_date.strip(), "%B %d, %Y")
                        .date()
                        .isoformat()
                    )
        except (ValueError, TypeError) as exc:
            self.stats.inc_value("dropped/bad_value")
            self.stats.inc_value(f"dropped/bad_value/{source}")
            raise DropItem(f"не удалось типизировать: {exc}")
        return item


class DedupPipeline:
    """Звено 300: дедупликация по бизнес-ключу.

    books  -> UPC
    quotes -> пара автор + текст цитаты (sha1)

    Повтор уровня 2 (страница автора) того же ключа не выбрасывается, а
    проходит как обновление: у него появились поля, которых нет у уже
    сохранённой записи (счётчик dedup/updates).
    """

    # поля, которые имеет право дополнить повторяющийся элемент
    MERGE_FIELDS = (
        "born_date",
        "born_date_raw",
        "born_location",
        "biography",
        "author_url",
        "description",
    )

    def __init__(self, stats):
        self.stats = stats
        self.seen = {}  # {source: {key: set(заполненных полей)}}

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.stats)

    @staticmethod
    def business_key(item):
        ad = ItemAdapter(item)
        if ad.get("source") == "books":
            return ad.get("upc")
        raw = f"{ad.get('author')}\x00{ad.get('quote')}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def process_item(self, item):
        source = item.get("source")
        key = self.business_key(item)
        if not key:
            raise DropItem("нет бизнес-ключа")
        filled = {k for k, v in ItemAdapter(item).items() if v not in (None, "")}
        bucket = self.seen.setdefault(source, {})
        if key not in bucket:
            bucket[key] = filled
            return item
        new_fields = (filled - bucket[key]) & set(self.MERGE_FIELDS)
        if new_fields:
            bucket[key] |= filled
            self.stats.inc_value("dedup/updates")
            return item
        self.stats.inc_value("duplicates/" + source)
        raise DropItem(f"дубль по бизнес-ключу {source}:{key}")


class SQLitePipeline:
    """Звено 400: пакетная запись в SQLite (две таблицы, общее соединение).

    Соединение — в open_spider/close_spider. Буфер сбрасывается executemany
    каждые BATCH записей; UPSERT по бизнес-ключу, у quotes поля сливаются
    через COALESCE (обновление уровня 2 не затирает данные уровня 1).
    """

    BATCH = 100
    BOOK_COLS = [
        "upc",
        "title",
        "url",
        "list_price",
        "rating",
        "price_excl_tax",
        "price_incl_tax",
        "tax",
        "availability",
        "quantity",
        "num_reviews",
        "category",
        "description",
        "source",
        "collected_at",
    ]
    QUOTE_COLS = [
        "quote",
        "author",
        "author_url",
        "tags",
        "born_date",
        "born_date_raw",
        "born_location",
        "biography",
        "source",
    ]

    def __init__(self, stats):
        self.stats = stats
        self.conn = None
        self.buf = []

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.stats)

    def open_spider(self):
        self.conn = sqlite3.connect("data/crawl.db")
        self.conn.executescript(SCHEMA)
        self.collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def process_item(self, item):
        self.buf.append(ItemAdapter(item).asdict())
        if len(self.buf) >= self.BATCH:
            self.flush()
        return item

    def flush(self):
        if not self.buf:
            return
        books = [r for r in self.buf if r.get("source") == "books"]
        quotes = [r for r in self.buf if r.get("source") == "quotes"]
        if books:
            cols = self.BOOK_COLS
            sql = (
                f"INSERT OR REPLACE INTO books ({', '.join(cols)}) VALUES "
                f"({', '.join('?' * len(cols))})"
            )
            self.conn.executemany(
                sql,
                [
                    tuple(
                        self.collected_at if c == "collected_at" else r.get(c)
                        for c in cols
                    )
                    for r in books
                ],
            )
        if quotes:
            cols = self.QUOTE_COLS + ["business_key", "collected_at"]
            updates = ", ".join(
                f"{c}=COALESCE(excluded.{c}, {c})" for c in self.QUOTE_COLS
            )
            sql = (
                f"INSERT INTO quotes ({', '.join(cols)}) VALUES "
                f"({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(business_key) DO UPDATE SET {updates}"
            )
            rows = [
                tuple(r.get(c) for c in self.QUOTE_COLS)
                + (DedupPipeline.business_key(r), self.collected_at)
                for r in quotes
            ]
            self.conn.executemany(sql, rows)
        self.conn.commit()
        n = len(self.buf)
        self.stats.inc_value("stored/rows", n)
        self.stats.inc_value("stored/flushes")
        self.buf = []

    def close_spider(self):
        self.flush()
        self.conn.close()
