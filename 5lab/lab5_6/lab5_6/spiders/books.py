"""Паук books.toscrape.com: пагинация каталога + обход карточек.

Уровень 1 (parse): список книг, пагинация через response.follow.
Уровень 2 (parse_item): карточка книги. Контекст уровня 1 (название,
цена списка, рейтинг) передаётся через cb_kwargs.
"""

import re
import json
import time
from pathlib import Path

import scrapy

from lab5_6.items import BookItem


class BooksSpider(scrapy.Spider):
    name = "books"
    allowed_domains = ["books.toscrape.com"]
    start_urls = ["https://books.toscrape.com/catalogue/page-1.html"]

    # числовой эквивалент словесного рейтинга в классе star-rating
    RATING = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

    def __init__(self, run_id=None, max_pages=0, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_id = run_id or time.strftime("books_%Y%m%d_%H%M%S")
        self.max_pages = int(max_pages)  # 0 — без лимита; лимит пагинации
        self.pages_done = 0
        self.started_at = time.time()

    def parse(self, response):
        self.pages_done += 1
        for pod in response.css("article.product_pod"):
            href = pod.css("h3 a::attr(href)").get()
            rating_cls = pod.css("p.star-rating::attr(class)").get("") or ""
            word = rating_cls.replace("star-rating", "").strip()
            yield scrapy.Request(
                response.urljoin(href),
                callback=self.parse_item,
                errback=self.on_error,
                cb_kwargs={
                    "title": pod.css("h3 a::attr(title)").get(),
                    "list_price": pod.css("p.price_color::text").get(),
                    "rating": self.RATING.get(word),
                },
            )
        # пагинация: семантический признак — кнопка "next" блока pager
        next_href = response.css("li.next a::attr(href)").get()
        if next_href and (not self.max_pages or self.pages_done < self.max_pages):
            yield response.follow(next_href, callback=self.parse, errback=self.on_error)

    def parse_item(self, response, title, list_price, rating):
        # карточка может быть нетипичной: все поля забираем через .get()
        # и позволяем ValidationPipeline решать, что обязательное
        item = BookItem()
        item["title"] = title
        item["list_price"] = list_price
        item["rating"] = rating
        item["list_url"] = response.url
        item["url"] = response.url
        item["source"] = "books"
        item["upc"] = response.xpath("//tr[th='UPC']/td/text()").get()
        item["price_excl_tax"] = response.xpath(
            "//tr[th='Price (excl. tax)']/td/text()"
        ).get()
        item["price_incl_tax"] = response.xpath(
            "//tr[th='Price (incl. tax)']/td/text()"
        ).get()
        item["tax"] = response.xpath("//tr[th='Tax']/td/text()").get()
        item["availability"] = response.xpath("//tr[th='Availability']/td/text()").get()
        item["num_reviews"] = response.xpath(
            "//tr[th='Number of reviews']/td/text()"
        ).get()
        item["category"] = response.css(
            "ul.breadcrumb li a[href*='/category/books/']::text"
        ).get()
        descr = response.css("#product_description + p::text").getall()
        item["description"] = " ".join(t.strip() for t in descr) or None
        yield item

    def on_error(self, failure):
        """Сетевая ошибка конкретного запроса: пишем в статистику, не роняем прогон."""
        self.crawler.stats.inc_value("network/errors")
        self.crawler.stats.inc_value(
            f"network/errors/{failure.request.url.split('/')[2]}"
        )
        self.logger.warning(
            "request failed: %s (%s)", failure.request.url, failure.value
        )

    def closed(self, reason):
        """Сохраняет статистику прогона в data/stats_<run_id>.json."""
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
