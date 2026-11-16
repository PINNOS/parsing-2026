"""Паук quotes.toscrape.com: пагинация цитат + страницы авторов.

Уровень 1 (parse): список цитат, пагинация.
Уровень 2 (parse_author): страница автора (дата/место рождения, биография).
Контекст уровня 1 (цитата, автор) передаётся через cb_kwargs, чтобы на
уровне 2 можно было дополнить уже собранную запись.
"""

import re
import json
import time
from pathlib import Path

import scrapy

from lab5_6.items import QuoteItem


class QuotesSpider(scrapy.Spider):
    name = "quotes"
    allowed_domains = ["quotes.toscrape.com"]
    start_urls = ["https://quotes.toscrape.com/page/1/"]

    def __init__(self, run_id=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_id = run_id or time.strftime("quotes_%Y%m%d_%H%M%S")
        self.started_at = time.time()

    def parse(self, response):
        for block in response.css("div.quote"):
            text = block.css("span.text::text").get()
            author = block.css("small.author::text").get()
            author_href = block.css("span a::attr(href)").get()
            tags = block.css("div.tags a.tag::text").getall()
            # сначала отдаём цитату, затем — страницу её автора (уровень 2)
            yield QuoteItem(
                quote=text,
                author=author,
                tags=tags,
                author_url=response.urljoin(author_href),
                source="quotes",
            )
            yield scrapy.Request(
                response.urljoin(author_href),
                callback=self.parse_author,
                errback=self.on_error,
                # ключ цитаты — пара автор+текст, поэтому страница автора
                # запрашивается для каждой цитаты: dupefilter схлопнул бы
                # повторы, и часть цитат осталась бы без данных автора
                dont_filter=True,
                cb_kwargs={"quote": text, "author": author},
            )
        next_href = response.css("li.next a::attr(href)").get()
        if next_href:
            yield response.follow(next_href, callback=self.parse, errback=self.on_error)

    def parse_author(self, response, quote, author):
        """Страница автора дополняет цитату: дата/место рождения, биография."""
        born_raw = response.css("span.author-born-date::text").get()
        yield QuoteItem(
            quote=quote,
            author=author,
            author_url=response.url,
            source="quotes",
            born_date_raw=born_raw,
            born_location=response.css("span.author-born-location::text").get(),
            biography=response.css("div.author-description::text").get(),
        )

    def on_error(self, failure):
        self.crawler.stats.inc_value("network/errors")
        self.logger.warning(
            "request failed: %s (%s)", failure.request.url, failure.value
        )

    def closed(self, reason):
        stats = dict(self.crawler.stats.get_stats())
        stats = {
            k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in stats.items()
        }
        out = {
            "run_id": self.run_id,
            "spider": self.name,
            "reason": reason,
            "elapsed_s": round(time.time() - self.started_at, 2),
            "stats": stats,
        }
        path = Path("data") / f"stats_{self.run_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger.info("stats saved to %s", path)
