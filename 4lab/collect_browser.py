"""Лаб. 4, вариант 6: браузерный сбор через Playwright (синхронный API).

Два тренировочных раздела (robots.txt проверен, см. отчёт):
  - load_more : scrapingcourse.com/button-click, цикл «Load more» (#load-more-btn), 147 товаров;
  - books     : books.toscrape.com, цикл пагинации «next», 4 страницы (80 книг).
Раздел scrapingcourse.com/ecommerce/ исключён: в robots.txt стоит Disallow: /ecommerce/*.

Запуск:
    python collect_browser.py                 # полный режим -> data_browser.csv, collection_log.csv
    python collect_browser.py --block-media   # замер с блокировкой медиа/шрифтов -> run_summary_blocked.json
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psutil
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

UA = "StudentScraperBot/1.0 (+https://github.com/PINNOS/parsing)"
AJAX_MARK = "/ajax/products"
BLOCKED_TYPES = {"image", "media", "font"}
SOURCES = ("load_more", "books")

LOAD_MORE = {
    "source": "load_more",
    "url": "https://www.scrapingcourse.com/button-click",
    "card": "#product-grid .product-item",
}
BOOKS = {
    "source": "books",
    "url": "https://books.toscrape.com/",
    "card": "article.product_pod",
    "next": "next",
    "max_pages": 4,
}
GROW_OR_HIDDEN = """([sel, n, id]) => document.querySelectorAll(sel).length > n
    || getComputedStyle(document.getElementById(id)).display === 'none'"""

HERE = Path(__file__).resolve().parent
ART = HERE / "artifacts"
LOGS = HERE / "logs"


class CollectError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def business_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def snapshot(page, name):
    ART.mkdir(exist_ok=True)
    page.screenshot(path=str(ART / f"{name}.png"))
    (ART / f"{name}.html").write_text(page.content(), encoding="utf-8")


def extract(page, card, source):
    cards = page.eval_on_selector_all(
        card,
        """
els => els.map(el => ({
    name:      el.querySelector('.product-name')?.innerText?.trim()
               ?? el.querySelector('h3 a')?.getAttribute('title') ?? null,
    price_raw: el.querySelector('.product-price')?.innerText?.trim()
               ?? el.querySelector('.price_color')?.innerText?.trim() ?? null,
    url:       el.querySelector('a')?.href ?? null,
    image_url: el.querySelector('img')?.src ?? null
}))""",
    )
    return [dict(c, source=source) for c in cards]


def tree_rss_mb():
    proc = psutil.Process(os.getpid())
    try:
        procs = [proc] + proc.children(recursive=True)
        return sum(p.memory_info().rss for p in procs) / 1024 / 1024
    except psutil.Error:
        return 0.0


def collect_load_more(page, args, cycle_rows, items, jlog, state):
    cfg = LOAD_MORE
    page.goto(cfg["url"], wait_until="domcontentloaded")
    page.wait_for_selector(cfg["card"])
    prev = page.locator(cfg["card"]).count()
    jlog("start", source=cfg["source"], initial_cards=prev)
    stalls = 0
    try:
        for it in range(1, args.max_clicks + 1):
            btn = page.get_by_role("button", name="Load more")
            if btn.count() == 0 or not btn.is_visible():
                jlog("end_of_source", source=cfg["source"], cycle=it)
                return
            t0 = time.perf_counter()
            try:
                with page.expect_response(
                    lambda r: AJAX_MARK in r.url, timeout=15_000
                ) as info:
                    btn.click()
                status = info.value.status
                page.wait_for_function(
                    GROW_OR_HIDDEN,
                    arg=[cfg["card"], prev, "load-more-btn"],
                    timeout=8_000,
                )
            except PWTimeout as e:
                snapshot(page, f"fail_{cfg['source']}_{it}")
                raise CollectError(
                    f"{cfg['source']} cycle {it}: {str(e).splitlines()[0][:120]}"
                )
            cur = page.locator(cfg["card"]).count()
            delta = cur - prev
            cycle_rows.append(
                {
                    "source": cfg["source"],
                    "cycle": it,
                    "elements": cur,
                    "delta": delta,
                    "duration_ms": round((time.perf_counter() - t0) * 1000),
                    "timestamp": utc_now(),
                    "url": info.value.url,
                    "http_status": status,
                }
            )
            jlog(
                "cycle",
                source=cfg["source"],
                cycle=it,
                elements=cur,
                delta=delta,
                http_status=status,
            )
            state["peak_mb"] = max(state["peak_mb"], tree_rss_mb())
            prev = cur
            stalls = stalls + 1 if delta <= 0 else 0
            if stalls >= args.stall_limit:
                jlog("stall_stop", source=cfg["source"], cycle=it)
                return
            time.sleep(args.pause)
        jlog("limit_clicks", source=cfg["source"], max_clicks=args.max_clicks)
    finally:
        items.extend(extract(page, cfg["card"], cfg["source"]))


def collect_paginated(cfg, max_pages, page, args, cycle_rows, items, jlog, state):
    page.goto(cfg["url"], wait_until="domcontentloaded")
    page.wait_for_selector(cfg["card"])
    for it in range(1, max_pages + 1):
        cards = extract(page, cfg["card"], cfg["source"])
        if not cards:
            jlog("stall_stop", source=cfg["source"], cycle=it)
            return
        items.extend(cards)
        cycle_rows.append(
            {
                "source": cfg["source"],
                "cycle": it,
                "elements": len(cards),
                "delta": len(cards),
                "duration_ms": 0,
                "timestamp": utc_now(),
                "url": page.url,
                "http_status": 200,
            }
        )
        jlog(
            "cycle",
            source=cfg["source"],
            cycle=it,
            elements=len(cards),
            page_url=page.url,
        )
        state["peak_mb"] = max(state["peak_mb"], tree_rss_mb())
        nxt = page.get_by_role("link", name=cfg["next"])
        if nxt.count() == 0:
            jlog("end_of_source", source=cfg["source"], cycle=it)
            return
        t0 = time.perf_counter()
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                nxt.first.click()
            page.wait_for_selector(cfg["card"])
        except PWTimeout as e:
            snapshot(page, f"fail_{cfg['source']}_{it}")
            raise CollectError(
                f"{cfg['source']} page {it}: {str(e).splitlines()[0][:120]}"
            )
        cycle_rows[-1]["duration_ms"] = round((time.perf_counter() - t0) * 1000)
        time.sleep(args.pause)
    jlog("limit_pages", source=cfg["source"], max_pages=max_pages)


def collect_books(page, args, cycle_rows, items, jlog, state):
    collect_paginated(
        BOOKS, BOOKS["max_pages"], page, args, cycle_rows, items, jlog, state
    )


def run(args):
    run_id = uuid.uuid4().hex[:8]
    mode = "blocked" if args.block_media else "full"
    LOGS.mkdir(exist_ok=True)
    jsonl_path = LOGS / f"run_browser_{mode}_{run_id}.jsonl"

    def jlog(event, **kw):
        row = {"ts": utc_now(), "run_id": run_id, "event": event, **kw}
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    t_start = time.perf_counter()
    responses, blocked, cycle_rows, items = [], [0], [], []
    state = {"peak_mb": 0.0}
    failed, exit_code = None, 0
    total_bytes = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=UA, viewport={"width": 1280, "height": 900}, locale="ru-RU"
            )
            context.set_default_timeout(15_000)
            context.set_default_navigation_timeout(30_000)
            page = context.new_page()

            if args.block_media:

                def router(route):
                    if route.request.resource_type in BLOCKED_TYPES:
                        blocked[0] += 1
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", router)
            page.on("response", lambda r: responses.append(r))

            for collector in (collect_load_more, collect_books):
                try:
                    collector(page, args, cycle_rows, items, jlog, state)
                except (CollectError, PWTimeout) as e:
                    failed = str(e)
                    jlog("failure", error=failed)
                    exit_code = 2
                    break

            records, seen = [], set()
            for it in items:
                key = business_key(it["name"])
                if key in seen:
                    continue
                seen.add(key)
                records.append(it)
            records = records[: args.max_records]

            for r in responses:
                total_bytes += int(r.headers.get("content-length", 0) or 0)

            if not args.block_media:
                collected = utc_now()
                with (HERE / "data_browser.csv").open(
                    "w", newline="", encoding="utf-8-sig"
                ) as f:
                    w = csv.DictWriter(
                        f,
                        fieldnames=[
                            "name",
                            "price",
                            "price_raw",
                            "url",
                            "image_url",
                            "source",
                            "collected_at",
                        ],
                    )
                    w.writeheader()
                    for r in records:
                        price = re.sub(r"[^\d.]", "", r["price_raw"] or "")
                        w.writerow(
                            {
                                "name": r["name"],
                                "price": price,
                                "price_raw": r["price_raw"],
                                "url": r["url"],
                                "image_url": r["image_url"],
                                "source": r["source"],
                                "collected_at": collected,
                            }
                        )
                with (HERE / "collection_log.csv").open(
                    "w", newline="", encoding="utf-8-sig"
                ) as f:
                    w = csv.DictWriter(
                        f,
                        fieldnames=[
                            "source",
                            "cycle",
                            "elements",
                            "delta",
                            "duration_ms",
                            "timestamp",
                            "url",
                            "http_status",
                        ],
                    )
                    w.writeheader()
                    w.writerows(cycle_rows)

            if len(records) < args.target and exit_code == 0:
                exit_code = 3
                jlog("below_target", records=len(records), target=args.target)
        finally:
            browser.close()

    elapsed = round(time.perf_counter() - t_start, 2)
    summary = {
        "mode": mode,
        "run_id": run_id,
        "records_unique": len(records),
        "records_raw": len(items),
        "by_source": {s: sum(1 for r in records if r["source"] == s) for s in SOURCES},
        "cycles": len(cycle_rows),
        "network_responses": len(responses),
        "ajax_responses": sum(1 for r in responses if AJAX_MARK in r.url),
        "traffic_mb": round(total_bytes / 1024 / 1024, 3),
        "blocked_requests": blocked[0],
        "elapsed_s": elapsed,
        "sec_per_record": round(elapsed / max(len(records), 1), 4),
        "peak_rss_mb": round(state["peak_mb"], 1),
        "failed": failed,
        "exit_code": exit_code,
    }
    (HERE / f"run_summary_{mode}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    jlog("summary", **summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--block-media", action="store_true")
    p.add_argument("--max-clicks", type=int, default=30)
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--max-records", type=int, default=400)
    p.add_argument("--target", type=int, default=200)
    p.add_argument("--stall-limit", type=int, default=2)
    p.add_argument("--pause", type=float, default=1.0)
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args(sys.argv[1:])))
