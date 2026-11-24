"""Лаб. 7, вариант 6. Доработанный сборщик: многодоменный сбор с лимитами по доменам.

База — собственный сборщик занятий 2/4/5 (requests + BeautifulSoup):
  * books.toscrape.com  — пагинация HTML-каталога (занятие 2);
  * quotes.toscrape.com — пагинация HTML-списка (занятие 5);
  * scrapingcourse.com/ajax/products — XHR-эндпоинт «Load more» (занятие 4).

Доработано до устойчивого пайплайна (занятие 7):
  * token bucket и адаптивная задержка ПО ДОМЕНАМ (`src/limiter.py`);
  * политика повторов: full jitter, cap, бюджет времени, приоритет Retry-After (`src/retry.py`);
  * предохранитель по доменам: closed → open → half-open → closed (`src/breaker.py`);
  * детектор блокировок и деградации, артефакты инцидентов (`src/detector.py`);
  * структурные JSON-логи и отчёт о прогоне с порогами (`src/observability.py`);
  * воспроизведение отказов — на локальной заглушке (`src/faultstub.py`).

Профили прогонов (пункт 4.7 задания):
  A — «наивный»: без лимита, повторов и условных запросов (антипаттерн, ≤ 30 запросов);
  B — «вежливый»: token bucket + адаптивная задержка + повторы + предохранитель + детектор;
  C — «вежливый + оптимизация»: B плюс условные запросы ETag/Last-Modified и gzip;
  `--incremental` дополнительно включает «водяной знак» (пропуск свежих URL).

Запуск:  python -m src.collect --profile B
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.breaker import BreakerRegistry  # noqa: E402
from src.detector import (  # noqa: E402
    append_history,
    inspect_response,
    load_history,
    save_incident,
)
from src.faultstub import start_stub, stop_stub  # noqa: E402
from src.limiter import (  # noqa: E402
    AdaptiveDelay,
    DomainLimits,
    DomainLimiters,
    plan_domain_workers,
)
from src.observability import (  # noqa: E402
    RunLogger,
    alerts_text,
    evaluate,
    load_log,
    metrics_from_log,
    write_report,
)
from src.retry import RetryPolicy, fetch_with_retry  # noqa: E402

LOGS = ROOT / "logs"
INCIDENTS = LOGS / "incidents"
STATE = ROOT / "state"
REPORTS = ROOT / "reports"
DATA = ROOT / "data"

UA = os.getenv(
    "SCRAPER_UA", "StudentScraperBot/1.0 (+https://github.com/PINNOS/parsing)"
)
PARSER_VERSION = "lab7/1.0"
STUB_HOST = "127.0.0.1:8765"
WATERMARK_TTL_H = 6.0

# Источники варианта 6: три реальных домена + локальная заглушка отказов.
SOURCES = [
    {"name": "stub-faults", "host": STUB_HOST, "kind": "faults", "pages": 0},
    {"name": "books", "host": "books.toscrape.com", "kind": "books", "pages": 5},
    {"name": "quotes", "host": "quotes.toscrape.com", "kind": "quotes", "pages": 5},
    {
        "name": "scrapingcourse",
        "host": "scrapingcourse.com",
        "kind": "ajax_products",
        "pages": 5,
    },
]

# Лимиты по доменам. Значения обоснованы в отчёте расчётом от латентности и объёма сбора.
# Лимиты по доменам. Значения обоснованы в отчёте расчётом от латентности и объёма сбора.
# Всплеск у содержательных доменов равен 1: пагинация последовательна, а burst > 1
# на коротком окне даёт скорость выше объявленной — в распределённом режиме это
# умножилось бы на число воркеров.
LIMITS = {
    "books.toscrape.com": DomainLimits(rate=0.5, burst=1, jitter=0.25),
    "quotes.toscrape.com": DomainLimits(rate=0.5, burst=1, jitter=0.25),
    "scrapingcourse.com": DomainLimits(rate=1.0, burst=1, jitter=0.2),
    STUB_HOST: DomainLimits(rate=2.0, burst=2, jitter=0.2),
}

FAULT_PATHS = [
    "/flaky",
    "/ratelimit-numeric",
    "/ratelimit-date",
    "/unauthorized",
    "/forbidden",
]
PROBE_PATH = "/ok"
STUB200_PATH = "/stub200"  # «тихая» заглушка с кодом 200 — для детектора
# Домены, где собираются данные: по ним и считаются качественные метрики.
CONTENT_HOSTS = tuple(s["host"] for s in SOURCES if s["kind"] != "faults")


# ───────────────────────── планирование задач ─────────────────────────


def source_urls(src) -> list[str]:
    pages = src["pages"]
    if src["kind"] == "books":
        return [
            f"https://books.toscrape.com/catalogue/page-{n}.html"
            for n in range(1, pages + 1)
        ]
    if src["kind"] == "quotes":
        return [f"https://quotes.toscrape.com/page/{n}/" for n in range(1, pages + 1)]
    if src["kind"] == "ajax_products":
        return [
            f"https://scrapingcourse.com/ajax/products?offset={10 * n}"
            for n in range(pages)
        ]
    return []


def build_plan(
    profile: str, include_stub200=False, only_hosts=None, stub_paths=None
) -> list[dict]:
    """Плоский список задач: отказы заглушки → сбор доменов → пробный запрос."""
    tasks = []
    hosts = set(only_hosts) if only_hosts else None

    if hosts is None or STUB_HOST in hosts:
        paths = (
            list(stub_paths)
            if stub_paths
            else FAULT_PATHS + ([STUB200_PATH] if include_stub200 else [])
        )
        tasks += [
            {
                "source": "stub-faults",
                "host": STUB_HOST,
                "kind": "faults",
                "phase": "faults",
                "url": f"http://{STUB_HOST}{p}",
            }
            for p in paths
        ]

    for src in SOURCES:
        if src["kind"] == "faults":
            continue
        if hosts is not None and src["host"] not in hosts:
            continue
        for url in source_urls(src):
            tasks.append(
                {
                    "source": src["name"],
                    "host": src["host"],
                    "kind": src["kind"],
                    "phase": "collect",
                    "url": url,
                }
            )

    # Пробный запрос после паузы: домен в open должен перейти half-open → closed.
    if profile in ("B", "C") and (hosts is None or STUB_HOST in hosts):
        tasks.append(
            {
                "source": "stub-faults",
                "host": STUB_HOST,
                "kind": "faults",
                "phase": "probe",
                "url": f"http://{STUB_HOST}{PROBE_PATH}",
            }
        )
    return tasks


# ───────────────────────── извлечение и валидация ─────────────────────────


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def extract_books(html: str, url: str) -> list[dict]:
    out = []
    for el in BeautifulSoup(html, "html.parser").select("article.product_pod"):
        link = el.select_one("h3 a")
        price_el = el.select_one("p.price_color")
        title = (link.get("title") if link else None) or ""
        href = link.get("href") if link else None
        data_id = None
        if href:
            found = re.search(r"_(\d+)/", href)
            data_id = found.group(1) if found else None
        out.append(
            {
                "key": data_id or slug(title),
                "title": " ".join(title.split()),
                "price_raw": " ".join(price_el.get_text().split())
                if price_el
                else None,
                "url": urljoin(url, href) if href else None,
            }
        )
    return out


def extract_quotes(html: str, url: str) -> list[dict]:
    out = []
    for el in BeautifulSoup(html, "html.parser").select("div.quote"):
        text_el = el.select_one("span.text")
        author_el = el.select_one("small.author")
        text = text_el.get_text(" ", strip=True) if text_el else None
        author = author_el.get_text(strip=True) if author_el else None
        key = hashlib.md5(f"{author}|{text}".encode("utf-8")).hexdigest()[:16]
        out.append({"key": key, "title": text, "author": author, "url": url})
    return out


def extract_products(html: str, url: str) -> list[dict]:
    out = []
    for el in BeautifulSoup(html, "html.parser").select(".product-item"):
        name_el = el.select_one(".product-name")
        price_el = el.select_one(".product-price")
        name = name_el.get_text(" ", strip=True) if name_el else None
        out.append(
            {
                "key": slug(name),
                "title": name,
                "price_raw": price_el.get_text(strip=True) if price_el else None,
                "url": url,
            }
        )
    return out


EXTRACTORS = {
    "books": extract_books,
    "quotes": extract_quotes,
    "ajax_products": extract_products,
}


def validate(item: dict) -> tuple[bool, str]:
    """Валидация записи: обязательны название и цена (для товарных источников)."""
    if not item.get("title"):
        return False, "нет названия"
    if "price_raw" in item and not item.get("price_raw"):
        return False, "нет цены"
    return True, ""


# ───────────────────────── состояние: ETag и водяной знак ─────────────────────────


def read_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def median(values):
    vals = sorted(v for v in values if v is not None)
    return vals[len(vals) // 2] if vals else None


def request_rows(log_path) -> list[dict]:
    df = load_log(log_path)
    if not len(df):
        return []
    req = df[df["event"] == "request"]
    return [
        {
            "host": row["host"],
            "bytes": int(row.get("bytes") or 0),
            "status": row.get("status"),
        }
        for _, row in req.iterrows()
    ]


def measure_rates(log_path) -> dict:
    """Фактическая скорость по домену: средний интервал между запросами из журнала."""
    df = load_log(log_path)
    out = {}
    if not len(df):
        return out
    req = df[df["event"] == "request"].copy()
    for host, part in req.groupby("host"):
        if len(part) < 3:
            continue
        ts = sorted(
            datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
            for t in part["ts"]
        )
        span = ts[-1] - ts[0]
        out[host] = {
            "requests": len(part),
            "span_s": round(span, 2),
            "avg_interval_s": round(span / (len(ts) - 1), 3) if span else None,
            "achieved_rate": round((len(ts) - 1) / span, 3) if span else None,
        }
    return out


# ───────────────────────── прогон ─────────────────────────


def run(
    profile: str,
    run_id=None,
    incremental=False,
    include_stub200=False,
    only_hosts=None,
    max_requests=0,
    timeout=15.0,
    use_stub=True,
    report_name=None,
    stub_paths=None,
    record_history=True,
) -> int:
    profile = profile.upper()
    run_id = run_id or uuid.uuid4().hex[:8]
    report_name = report_name or f"run_report_{profile}.json"
    for folder in (LOGS, INCIDENTS, STATE, REPORTS, DATA):
        folder.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"run_{run_id}.jsonl"
    if log_path.exists():
        log_path.unlink()

    naive = profile == "A"
    conditional = profile == "C"

    plan = build_plan(
        profile,
        include_stub200=include_stub200,
        only_hosts=only_hosts,
        stub_paths=stub_paths,
    )
    if max_requests:
        plan = plan[:max_requests]

    server = (
        start_stub()
        if (use_stub and any(t["host"] == STUB_HOST for t in plan))
        else None
    )

    limiter = None
    if not naive:
        limiter = DomainLimiters(
            limits=LIMITS,
            adaptive=AdaptiveDelay(
                target_concurrency=1.0, start=0.0, min_delay=0.0, max_delay=5.0
            ),
        )
    breaker = None if naive else BreakerRegistry(threshold=3, reset_after=8.0)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Encoding": "identity" if naive else "gzip, deflate",
        }
    )

    logger = RunLogger(
        log_path,
        run_id,
        profile=profile,
        user_agent=UA,
        parser_version=PARSER_VERSION,
        plan_urls=len(plan),
        naive=naive,
        conditional=conditional,
        incremental=incremental,
        stub=bool(server),
    )
    policy = RetryPolicy(
        max_attempts=1 if naive else 4,
        base=0.4,
        cap=6.0,
        budget_s=30.0,
        timeout=timeout,
    )

    etags = read_json(STATE / "etags.json", {})
    watermark = read_json(STATE / "watermark.json", {"seen": {}, "runs": 0})
    seen = watermark.get("seen", {})

    collected, dropped, reused, suspicious, skipped, skipped_wm = 0, 0, 0, 0, 0, 0
    blocked_sources, incidents = set(), 0
    items_out = []
    started = time.perf_counter()

    for task in plan:
        host, url, kind = task["host"], task["url"], task["kind"]
        if task["source"] in blocked_sources:
            logger.log(
                "skip", host=host, url=url, reason="источник остановлен детектором"
            )
            continue

        if breaker is not None and not breaker.allow(host):
            skipped += 1
            logger.log(
                "breaker_skip",
                host=host,
                url=url,
                status=None,
                state=breaker.for_host(host).state,
                reason="домен изолирован предохранителем",
            )
            continue

        if incremental and url in seen:  # водяной знак: свежий URL не трогаем вовсе
            age_h = (time.time() - seen[url]["ts"]) / 3600.0
            if age_h < WATERMARK_TTL_H:
                skipped_wm += 1
                logger.log(
                    "skip",
                    host=host,
                    url=url,
                    reason="водяной знак",
                    age_h=round(age_h, 3),
                )
                continue

        headers = None
        if conditional and url in etags:
            stored = etags[url]
            headers = {}
            if stored.get("etag"):
                headers["If-None-Match"] = stored["etag"]
            if stored.get("last_modified"):
                headers["If-Modified-Since"] = stored["last_modified"]
            headers = headers or None

        state_before = breaker.for_host(host).state if breaker is not None else None
        resp, _attempts = fetch_with_retry(
            session, url, policy, logger, limiter=limiter, headers=headers
        )
        if resp is None:
            if breaker is not None:
                breaker.record(host, False)
            logger.log("stop", host=host, url=url, reason="нет ответа после повторов")
            continue

        status = resp.status_code
        seen[url] = {"ts": time.time(), "status": status}
        if breaker is not None:
            breaker.record(host, status < 400)
            if breaker.for_host(host).state == "open" and state_before != "open":
                logger.log(
                    "breaker_open",
                    host=host,
                    url=url,
                    status=status,
                    state="open",
                    activations=breaker.for_host(host).activations,
                    reason="порог последовательных отказов достигнут",
                )

        if status == 304:  # условный запрос: тело не передавалось, данные прежние
            reused += count_cached(url, run_id)
            logger.log("cache", host=host, url=url, status=304, reused=reused)
            seen[url] = {"ts": time.time(), "status": status}
            continue

        # Извлекаем записи до вердикта: по их числу проверяется, что страница не пустая.
        raw_items = (
            EXTRACTORS[kind](resp.text, url)
            if kind in EXTRACTORS and status < 400
            else None
        )
        if naive:
            # Профиль A намеренно оставлен «как было»: детектора в нём нет,
            # 403/429/503 просто игнорируются — это и есть предмет сравнения.
            verdict, signals = "", []
        else:
            verdict, signals = inspect_response(
                resp,
                expected_selector_count=(0 if raw_items == [] else None),
                min_bytes=1000,
            )
        if signals:
            logger.log(
                "detect",
                host=host,
                url=url,
                status=status,
                verdict=verdict,
                signals="; ".join(signals),
            )
        if verdict == "блокировка":
            incidents += 1
            blocked_sources.add(task["source"])
            incident = save_incident(
                INCIDENTS,
                f"{task['source']}_{run_id}",
                resp,
                meta={
                    "verdict": verdict,
                    "signals": signals,
                    "run_id": run_id,
                    "profile": profile,
                },
            )
            logger.log(
                "incident",
                host=host,
                url=url,
                status=status,
                verdict=verdict,
                error="; ".join(signals),
                artifacts=incident.get("body"),
            )
            continue
        if verdict == "подозрение":
            suspicious += 1

        if status >= 400:  # 401/403/429 разобраны выше, данных на странице нет
            continue

        if conditional:
            etag = resp.headers.get("ETag")
            last_mod = resp.headers.get("Last-Modified")
            if etag or last_mod:
                etags[url] = {
                    "etag": etag,
                    "last_modified": last_mod,
                    "checked_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
        if raw_items is not None:
            logger.log("page", host=host, url=url, status=status, items=len(raw_items))
            for raw in raw_items:
                ok, reason = validate(raw)
                if not ok:
                    dropped += 1
                    logger.log("drop", host=host, url=url, reason=reason)
                    continue
                collected += 1
                items_out.append(item_record(raw, kind, url, run_id))

    if items_out:  # слой данных: append-only JSONL
        with (DATA / "items.jsonl").open("a", encoding="utf-8") as fh:
            for rec in items_out:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    write_json(STATE / "etags.json", etags)
    write_json(
        STATE / "watermark.json",
        {
            "seen": seen,
            "runs": int(watermark.get("runs", 0)) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    if server is not None:
        stop_stub(server)

    duration = round(time.perf_counter() - started, 2)
    rows = request_rows(log_path)
    avg_bytes = round(sum(r["bytes"] for r in rows) / len(rows), 1) if rows else 0.0
    # Размер ответа и доля ошибок считаются отдельно по трём содержательным доменам:
    # заглушка инжектит отказы намеренно и исказила бы сравнение с историей.
    content_rows = [r for r in rows if r["host"] in CONTENT_HOSTS]
    page_rows = [r for r in content_rows if r["status"] == 200]
    avg_page_bytes = (
        round(sum(r["bytes"] for r in page_rows) / len(page_rows), 1)
        if page_rows
        else None
    )
    content_errors = sum(
        1 for r in content_rows if r["status"] is None or r["status"] >= 400
    )
    records_total = collected + reused
    history_path = REPORTS / "history.json"
    history = [h for h in load_history(history_path) if h.get("run") != run_id]
    med_records = median([h.get("records", 0) for h in history]) if history else None
    med_bytes = median([h.get("avg_page_bytes") for h in history]) if history else None
    med_pages = median([h.get("pages") for h in history]) if history else None
    # Средний размер страницы сравнивается с историей только если страниц скачано
    # сопоставимо много: при условных запросах часть страниц приходит без тела (304),
    # и подвыборка загруженных страниц смещена.
    comparable = med_pages is None or len(page_rows) >= 0.8 * med_pages
    avg_size_pct = (
        round(avg_page_bytes / med_bytes * 100, 1)
        if med_bytes and avg_page_bytes and comparable
        else None
    )
    extras = {
        "profile": profile,
        "collected": collected,
        "records_total": records_total,
        "dropped": dropped,
        "dropped_pct": round(dropped / (collected + dropped) * 100, 2)
        if collected + dropped
        else 0.0,
        "reused_from_304": reused,
        "suspicious_responses": suspicious,
        "skipped_by_watermark": skipped_wm,
        "skipped_by_breaker": skipped,
        "incidents": incidents,
        "block_incidents": incidents,
        "duration_s": duration,
        "avg_response_bytes": avg_bytes,
        "avg_page_bytes": avg_page_bytes,
        "content_pages": len(page_rows),
        "error_rate_content_domains_pct": (
            round(content_errors / len(content_rows) * 100, 2) if content_rows else None
        ),
        "limits": {
            h: {"rate": lim.rate, "burst": lim.burst, "jitter": lim.jitter}
            for h, lim in LIMITS.items()
        },
        "domains_configured": len(LIMITS),
        "distributed_mode": (
            "подмножество доменов (--only-hosts)" if only_hosts else "один процесс"
        ),
        "achieved_rate_per_host": measure_rates(log_path),
        "history_runs": len(history),
        # Сравнение с историей неприменимо к прогону, который по определению не
        # загружает данные повторно (инкрементальный режим) — пороги отключаются.
        "history_comparison": not incremental,
        "median_records": med_records,
        "median_avg_page_bytes": med_bytes,
        "records_vs_median_pct": (
            round(records_total / med_records * 100, 1) if med_records else None
        ),
        "avg_size_vs_median_pct": avg_size_pct,
        "run_id": run_id,
        "report": report_name,
    }
    metrics = metrics_from_log(log_path, extras)
    alerts, status = evaluate(metrics)
    exit_code = 3 if incidents else (2 if status == "красный" else 0)
    metrics["exit_code"] = exit_code
    write_report(REPORTS / report_name, metrics, alerts, status)
    if record_history:
        append_history(
            history_path,
            {
                "run": run_id,
                "profile": profile,
                "records": records_total,
                "avg_page_bytes": avg_page_bytes,
                "pages": len(page_rows),
                "status": status,
                "duration_s": duration,
            },
        )
    logger.log(
        "summary",
        status=status,
        alerts=[a["metric"] for a in alerts],
        **{k: v for k, v in extras.items() if k != "limits"},
    )
    print(
        json.dumps(
            {
                "profile": profile,
                "run_id": run_id,
                "status": status,
                "alerts": alerts_text(alerts),
                "requests": metrics["requests"],
                "collected": collected,
                "dropped": dropped,
                "breaker_activations": metrics["breaker_activations"],
                "incidents": incidents,
                "duration_s": duration,
                "report": str(REPORTS / report_name),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


def count_cached(url: str, run_id: str) -> int:
    """Сколько РАЗНЫХ записей по этому URL уже собрано ранее (для ответа 304).

    Уникальность — по бизнес-ключу (`id`), а не по числу строк: файл журнала
    дописывается каждым прогоном, поэтому одинаковые записи в нём повторяются.
    """
    path = DATA / "items.jsonl"
    if not path.exists():
        return 0
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("_source_url") == url and rec.get("_run_id") != run_id:
            ids.add(rec.get("id"))
    return len(ids)


def item_record(raw: dict, kind: str, url: str, run_id: str) -> dict:
    key = raw.get("key") or slug(raw.get("title", ""))
    return {
        "id": f"{kind}:{key}",
        "source": kind,
        "title": raw.get("title"),
        "author": raw.get("author"),
        "price_raw": raw.get("price_raw"),
        "url": raw.get("url"),
        "_source_url": url,
        "_collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_parser_version": PARSER_VERSION,
        "_run_id": run_id,
        "_status": "ok",
    }


# ───────────────────────── распределённый прогон ─────────────────────────


def run_distributed(
    profile: str, run_id: str, workers: int, pinning: bool = True
) -> int:
    """Распределённый прогон: N процессов.

    `pinning=True` — «один воркер на домен»: домен закреплён ровно за одним процессом,
    поэтому суммарная скорость на домен равна объявленному лимиту.
    `pinning=False` — антипаттерн: каждый процесс обходит все домены, и лимит
    умножается на число воркеров (это и показывают замеры).
    """
    hosts = [s["host"] for s in SOURCES if s["kind"] != "faults"]
    plan = (
        plan_domain_workers(hosts, workers)
        if pinning
        else [{"worker": i, "host": h} for i in range(workers) for h in hosts]
    )
    procs = []
    for idx in range(max(1, int(workers))):
        assigned = [p["host"] for p in plan if p["worker"] == idx]
        if not assigned:
            continue
        cmd = [
            sys.executable,
            "-m",
            "src.collect",
            "--profile",
            profile,
            "--run-id",
            f"{run_id}_w{idx}",
            "--only-hosts",
            ",".join(assigned),
            "--no-stub",
            "--no-history",
            "--report-name",
            f"run_report_{run_id}_w{idx}.json",
        ]
        procs.append(
            subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        )
    started = time.perf_counter()
    for proc in procs:
        proc.wait()
    load_time = round(time.perf_counter() - started, 2)

    merged = LOGS / f"run_{run_id}.jsonl"
    if merged.exists():
        merged.unlink()
    with merged.open("a", encoding="utf-8") as out:
        for idx in range(max(1, int(workers))):
            part = LOGS / f"run_{run_id}_w{idx}.jsonl"
            if part.exists():
                out.write(part.read_text(encoding="utf-8"))
    rates = measure_rates(merged)
    tolerance_pct = 5.0  # допуск измерения: интервалы считаются по меткам времени
    over = {
        host: {
            "achieved_rate": r["achieved_rate"],
            "limit": LIMITS[host].rate,
            "over_pct": round((r["achieved_rate"] / LIMITS[host].rate - 1) * 100, 1),
        }
        for host, r in rates.items()
        if host in LIMITS
        and r["achieved_rate"] > LIMITS[host].rate * (1 + tolerance_pct / 100)
    }
    report = {
        "run_id": run_id,
        "profile": profile,
        "workers": int(workers),
        "mode": (
            "один воркер на домен"
            if pinning
            else "без закрепления домена (антипаттерн)"
        ),
        "pinning": pinning,
        "assignment": plan,
        "duration_s": load_time,
        "achieved_rates": rates,
        "declared_limits": {h: lim.rate for h, lim in LIMITS.items()},
        "tolerance_pct": tolerance_pct,
        "limit_violations": over,
        "within_limits": not over if rates else None,
    }
    write_json(REPORTS / f"distributed_{run_id}.json", report)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "mode": report["mode"],
                "workers": report["workers"],
                "within_limits": report["within_limits"],
                "limit_violations": over,
                "duration_s": load_time,
                "achieved_rates": rates,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Лаб. 7, вариант 6: многодоменный вежливый сбор"
    )
    p.add_argument("--profile", default="B", choices=["A", "B", "C", "a", "b", "c"])
    p.add_argument("--run-id", default=None)
    p.add_argument("--report-name", default=None, help="имя файла отчёта в reports/")
    p.add_argument(
        "--incremental",
        action="store_true",
        help="включить водяной знак (пропуск свежих URL)",
    )
    p.add_argument(
        "--include-stub200",
        action="store_true",
        help="добавить «тихую» заглушку с кодом 200 для демонстрации детектора",
    )
    p.add_argument(
        "--only-hosts",
        default=None,
        help="обрабатывать только перечисленные домены (распределённый прогон)",
    )
    p.add_argument("--max-requests", type=int, default=0)
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        help="N>0 — распределённый прогон: N процессов (по умолчанию один воркер на домен)",
    )
    p.add_argument(
        "--no-pinning",
        action="store_true",
        help="в распределённом прогоне не закреплять домен за воркером (антипаттерн)",
    )
    p.add_argument(
        "--no-stub", action="store_true", help="не поднимать локальную заглушку"
    )
    p.add_argument(
        "--stub-paths",
        default=None,
        help="явный список путей заглушки через запятую, например /stub200,/forbidden",
    )
    p.add_argument(
        "--no-history",
        action="store_true",
        help="не добавлять прогон в историю сравнений (служебные и инкрементальные прогоны)",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    only_hosts = args.only_hosts.split(",") if args.only_hosts else None
    stub_paths = args.stub_paths.split(",") if args.stub_paths else None
    if args.workers > 0:
        return run_distributed(
            args.profile,
            args.run_id or uuid.uuid4().hex[:8],
            args.workers,
            pinning=not args.no_pinning,
        )
    return run(
        args.profile,
        run_id=args.run_id,
        incremental=args.incremental,
        include_stub200=args.include_stub200,
        only_hosts=only_hosts,
        max_requests=args.max_requests,
        timeout=args.timeout,
        use_stub=not args.no_stub,
        report_name=args.report_name,
        stub_paths=stub_paths,
        record_history=not args.no_history,
    )


if __name__ == "__main__":
    raise SystemExit(main())
