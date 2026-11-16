"""Лаб. 4, вариант 6: дешёвая альтернатива без браузера (requests + BeautifulSoup).

  - load_more  : найденный через перехват сети XHR GET /ajax/products?offset=N (шаг 10, как data-offset);
  - books      : статический HTML books.toscrape.com, переход по ссылке next (4 страницы).
Раздел /ecommerce/ исключён по robots.txt (Disallow: /ecommerce/*).
Результат: data_light.csv, сверка с data_browser.csv (reconcile_light_vs_browser.json).
"""

import argparse
import csv
import email.utils
import json
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import psutil
import requests
from bs4 import BeautifulSoup

UA = "StudentScraperBot/1.0 (+https://github.com/PINNOS/parsing)"
LM_API = "https://www.scrapingcourse.com/ajax/products"
BOOKS_URL = "https://books.toscrape.com/"
RETRY_STATUS = {429, 500, 502, 503, 504}
FIELDS = [
    "name",
    "price",
    "price_raw",
    "url",
    "image_url",
    "source",
    "method",
    "collected_at",
]

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def business_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def retry_delay(attempt, retry_after, base=0.5, cap=8.0):
    if retry_after:
        if retry_after.isdigit():
            return float(retry_after)
        try:
            when = email.utils.parsedate_to_datetime(retry_after)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass
    return random.uniform(0, min(cap, base * 2 ** (attempt - 1)))


class Client:
    def __init__(self, jlog, retries=3):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.jlog = jlog
        self.retries = retries
        self.stats = {"requests": 0, "bytes": 0, "retries": 0}

    def get(self, url, params=None):
        for attempt in range(1, self.retries + 1):
            t0 = time.perf_counter()
            try:
                r = self.s.get(url, params=params, timeout=15)
            except requests.RequestException as e:
                self.jlog("network_error", url=url, attempt=attempt, error=str(e)[:160])
                return None
            if "charset" not in r.headers.get("Content-Type", ""):
                r.encoding = "utf-8"
            ms = round((time.perf_counter() - t0) * 1000)
            self.stats["requests"] += 1
            self.stats["bytes"] += len(r.content)
            self.jlog(
                "http",
                url=r.url,
                status=r.status_code,
                attempt=attempt,
                bytes=len(r.content),
                ms=ms,
            )
            if r.status_code not in RETRY_STATUS or attempt == self.retries:
                return r
            delay = retry_delay(attempt, r.headers.get("Retry-After"))
            self.stats["retries"] += 1
            self.jlog(
                "retry",
                url=url,
                status=r.status_code,
                attempt=attempt,
                delay_s=round(delay, 2),
            )
            time.sleep(delay)
        return r


def parse_cards(html, base_url, card_sel, source, method):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for el in soup.select(card_sel):
        a = el.select_one("a[href]")
        name = el.select_one(".product-name")
        title = el.select_one("h3 a")
        price = el.select_one(".product-price") or el.select_one(".price_color")
        img = el.select_one("img[src]")
        out.append(
            {
                "name": " ".join(
                    (name.get_text() if name else (title.get("title") or "")).split()
                )
                or None,
                "price_raw": " ".join(price.get_text().split()) if price else None,
                "url": urljoin(base_url, a["href"]) if a else None,
                "image_url": urljoin(base_url, img["src"]) if img else None,
                "source": source,
                "method": method,
            }
        )
    return out


def collect_load_more(client, args, jlog):
    items, offset = [], 0
    for _ in range(args.max_pages):
        r = client.get(LM_API, params={"offset": offset})
        if r is None:
            jlog("stop", source="load_more", reason="network_error", offset=offset)
            break
        if r.status_code != 200:
            jlog("stop", source="load_more", reason=f"status {r.status_code}")
            break
        cards = parse_cards(r.text, LM_API, ".product-item", "load_more", "xhr")
        if not cards:
            jlog("stop", source="load_more", reason="empty_fragment", offset=offset)
            break
        items.extend(cards)
        jlog("page", source="load_more", offset=offset, cards=len(cards))
        offset += 10
        time.sleep(args.pause)
    return items


def collect_html_pages(
    client, args, jlog, start_url, card_sel, next_sel, source, max_pages
):
    items, url = [], start_url
    for n in range(1, max_pages + 1):
        r = client.get(url)
        if r is None:
            jlog("stop", source=source, reason="network_error", page=n)
            break
        if r.status_code != 200:
            jlog("stop", source=source, reason=f"status {r.status_code}", page=n)
            break
        cards = parse_cards(r.text, url, card_sel, source, "html")
        if not cards:
            jlog("stop", source=source, reason="no_cards", page=n)
            break
        items.extend(cards)
        jlog("page", source=source, page=n, cards=len(cards))
        nxt = BeautifulSoup(r.text, "html.parser").select_one(next_sel)
        if nxt is None or not nxt.get("href"):
            jlog("end_of_source", source=source, page=n)
            break
        url = urljoin(url, nxt["href"])
        time.sleep(args.pause)
    return items


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = business_key(it["name"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def reconcile(light, browser_csv):
    if not browser_csv.exists():
        return {"status": "no_browser_data"}
    with browser_csv.open(encoding="utf-8-sig") as f:
        browser = list(csv.DictReader(f))
    b = {business_key(r["name"]): r for r in browser}
    l = {business_key(r["name"]): r for r in light}
    only_b = sorted(set(b) - set(l))
    only_l = sorted(set(l) - set(b))
    both = set(b) & set(l)
    name_diff = [k for k in both if (b[k]["name"] or "") != (l[k]["name"] or "")]
    price_diff = [k for k in both if (b[k]["price"] or "") != (l[k]["price"] or "")]
    url_diff = [k for k in both if (b[k]["url"] or "") != (l[k]["url"] or "")]
    return {
        "browser_unique": len(b),
        "light_unique": len(l),
        "matched_by_business_key": len(both),
        "only_browser": len(only_b),
        "only_light": len(only_l),
        "name_mismatch": len(name_diff),
        "price_mismatch_numeric": len(price_diff),
        "url_mismatch": len(url_diff),
        "examples_only_browser": [b[k]["name"] for k in only_b[:5]],
        "examples_only_light": [l[k]["name"] for k in only_l[:5]],
        "examples_url_mismatch": [
            {"name": b[k]["name"], "browser": b[k]["url"], "light": l[k]["url"]}
            for k in url_diff[:5]
        ],
    }


def run(args):
    run_id = uuid.uuid4().hex[:8]
    LOGS.mkdir(exist_ok=True)
    jsonl_path = LOGS / f"run_light_{run_id}.jsonl"

    def jlog(event, **kw):
        row = {"ts": utc_now(), "run_id": run_id, "event": event, **kw}
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    t0 = time.perf_counter()
    client = Client(jlog, retries=args.retries)
    raw = collect_load_more(client, args, jlog) + collect_html_pages(
        client,
        args,
        jlog,
        BOOKS_URL,
        "article.product_pod",
        "li.next a",
        "books",
        4,
    )
    records = dedupe(raw)[: args.max_records]
    elapsed = round(time.perf_counter() - t0, 2)

    collected = utc_now()
    with (HERE / "data_light.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in records:
            price = re.sub(r"[^\d.]", "", r["price_raw"] or "")
            w.writerow({**r, "price": price, "collected_at": collected})

    rec = reconcile(
        [{**r, "price": re.sub(r"[^\d.]", "", r["price_raw"] or "")} for r in records],
        HERE / "data_browser.csv",
    )
    (HERE / "reconcile_light_vs_browser.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = {
        "mode": "light",
        "run_id": run_id,
        "records_unique": len(records),
        "records_raw": len(raw),
        "by_source": {
            s: sum(1 for r in records if r["source"] == s)
            for s in ("load_more", "books")
        },
        "requests": client.stats["requests"],
        "retries": client.stats["retries"],
        "traffic_mb": round(client.stats["bytes"] / 1024 / 1024, 3),
        "elapsed_s": elapsed,
        "sec_per_record": round(elapsed / max(len(records), 1), 4),
        "peak_rss_mb": round(psutil.Process().memory_info().rss / 1024 / 1024, 1),
        "reconcile": {
            k: rec.get(k)
            for k in (
                "matched_by_business_key",
                "only_browser",
                "only_light",
                "name_mismatch",
                "price_mismatch_numeric",
                "url_mismatch",
            )
        },
        "exit_code": 0 if len(records) >= args.target else 3,
    }
    (HERE / "run_summary_light.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    jlog("summary", **summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary["exit_code"]


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--max-pages", type=int, default=30)
    p.add_argument("--max-records", type=int, default=400)
    p.add_argument("--target", type=int, default=200)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--pause", type=float, default=1.0)
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args(sys.argv[1:])))
