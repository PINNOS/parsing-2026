PRAGMA foreign_keys = ON;

-- Таблица товаров: первичный ключ — бизнес-ключ (не URL)
CREATE TABLE IF NOT EXISTS products (
  product_key   TEXT PRIMARY KEY,   -- бизнес-ключ (стабильный)
  title         TEXT NOT NULL,
  price_cents   INTEGER NOT NULL CHECK (price_cents > 0),  -- деньги в минорных единицах (копейках): без float
  stock         INTEGER CHECK (stock IS NULL OR stock >= 0),
  rating        INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
  category      TEXT,
  in_stock      INTEGER NOT NULL,
  source        TEXT NOT NULL,
  source_url    TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  collected_at  TEXT NOT NULL,     -- ISO8601 UTC
  updated_at    TEXT NOT NULL      -- ISO8601 UTC
);

-- Представление с ценой в привычном виде (только для чтения/отображения)
CREATE VIEW IF NOT EXISTS products_v AS
SELECT product_key, title, price_cents / 100.0 AS price, stock, rating, category,
       in_stock, source, source_url, parser_version, collected_at, updated_at
FROM products;

-- Индексы под фактические запросы
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_price_cents ON products(price_cents);
CREATE INDEX IF NOT EXISTS idx_products_source ON products(source);

-- Таблица истории (снапшоты по прогонам)
CREATE TABLE IF NOT EXISTS product_history (
  product_key  TEXT NOT NULL,
  observed_at  TEXT NOT NULL,
  price_cents  INTEGER NOT NULL,
  stock        INTEGER,
  rating       INTEGER,
  in_stock     INTEGER,
  title        TEXT,
  category     TEXT,
  PRIMARY KEY (product_key, observed_at)
);

-- Отбракованные записи
CREATE TABLE IF NOT EXISTS rejects (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  product_key  TEXT,
  source       TEXT,
  reason       TEXT NOT NULL,
  payload      TEXT,
  seen_at      TEXT NOT NULL
);
