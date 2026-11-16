"""
Контракт данных (товарный каталог), Pydantic v2.

Исполняемая версия этого же контракта используется в ноутбуке `6lab.ipynb`
(раздел «Контракт данных»); здесь модуль вынесен для переиспользования и проверки.
Деньги — `Decimal` (не `float`), у каждой записи — метаданные происхождения.
"""

from datetime import datetime, timezone
from decimal import Decimal
import unicodedata

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator


class Product(BaseModel):
    product_key: str = Field(min_length=1, max_length=200)  # бизнес-ключ
    title: str = Field(min_length=1, max_length=300)
    price: Decimal = Field(
        gt=0, le=10_000, decimal_places=2
    )  # деньги — Decimal, не float
    stock: int | None = Field(default=None, ge=0, le=1_000_000)
    rating: int | None = Field(default=None, ge=1, le=5)
    category: str | None = None
    in_stock: bool
    source: str
    source_url: str
    parser_version: str
    collected_at: datetime

    @field_validator("title")
    @classmethod
    def v_title(cls, v: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", v).split())

    @field_validator("source_url")
    @classmethod
    def v_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL должен начинаться с http(s)://")
        return v.split("?")[0]

    @field_validator("category")
    @classmethod
    def v_cat(cls, v):
        if v is None:
            return v
        return " ".join(unicodedata.normalize("NFKC", v).split()) or None

    @model_validator(mode="after")
    def v_consistency(self):
        # согласованность полей: нельзя быть «в наличии» без остатка
        if self.in_stock and self.stock is not None and self.stock == 0:
            raise ValueError("несогласованно: in_stock=True при stock=0")
        return self


def row2payload(r) -> dict:
    """Строка очищенного слоя -> аргументы для `Product`."""
    price = (
        Decimal(str(round(float(r["price"]), 2)))
        if pd.notna(r["price"])
        else Decimal("0")
    )
    ca = r["collected_at_dt"]
    return {
        "product_key": r["product_key"],
        "title": r["title"],
        "price": price,
        "stock": None if pd.isna(r["stock"]) else int(r["stock"]),
        "rating": None if pd.isna(r["rating"]) else int(r["rating"]),
        "category": r.get("category"),
        "in_stock": bool(r["in_stock"]),
        "source": r["_source"],
        "source_url": r["detail_url"] or r["_source_url"],
        "parser_version": r["_parser_version"],
        "collected_at": ca.to_pydatetime()
        if pd.notna(ca)
        else datetime.now(timezone.utc),
    }
