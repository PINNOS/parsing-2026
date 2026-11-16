-- Схема лаб. 6 для PostgreSQL (вариант no-code: n8n upsert/insert)
-- Первичный ключ — бизнес-ключ product_key; UPSERT по нему.

CREATE TABLE IF NOT EXISTS products (
  product_key    TEXT PRIMARY KEY,
  title          TEXT NOT NULL CHECK (length(title) > 0),
  price          NUMERIC(12,2) NOT NULL CHECK (price > 0),
  stock          INTEGER CHECK (stock IS NULL OR stock >= 0),
  rating         INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
  category       TEXT,
  in_stock       BOOLEAN NOT NULL,
  source         TEXT NOT NULL,
  source_url     TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  collected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);
CREATE INDEX IF NOT EXISTS idx_products_price    ON products (price);

CREATE TABLE IF NOT EXISTS rejects (
  id          BIGSERIAL PRIMARY KEY,
  product_key TEXT,
  source      TEXT,
  reason      TEXT NOT NULL,
  payload     JSONB,
  seen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
