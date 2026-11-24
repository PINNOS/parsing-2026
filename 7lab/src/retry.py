"""Лаб. 7, вариант 6. Политика повторов: классификация ошибок, backoff с full jitter,
cap, лимит попыток, бюджет времени на URL и приоритет заголовка `Retry-After`.

Правило, из которого всё следует: повторяются только ВРЕМЕННЫЕ сбои.
4xx (кроме 429) повторять бессмысленно и вредно; 401/403 — не техническая помеха,
а сигнал о доступе или возможной блокировке, поэтому фиксируются без повторов.
"""

from __future__ import annotations

import email.utils
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# Классификация: код/исключение → класс ошибки → повторять или нет.
CLASSES = [
    {
        "ситуация": "200–299",
        "класс": "успех",
        "повторять": "нет",
        "действие": "обработать ответ",
    },
    {
        "ситуация": "304",
        "класс": "не изменилось (кэш)",
        "повторять": "нет",
        "действие": "взять данные из прошлого прогона",
    },
    {
        "ситуация": "429",
        "класс": "ограничение скорости",
        "повторять": "да",
        "действие": "Retry-After, иначе backoff; снизить лимит домена",
    },
    {
        "ситуация": "500, 502, 503, 504",
        "класс": "сбой сервера",
        "повторять": "да",
        "действие": "backoff с full jitter; при серии — предохранитель",
    },
    {
        "ситуация": "таймаут, разрыв соединения",
        "класс": "временная сетевая ошибка",
        "повторять": "да",
        "действие": "backoff с full jitter",
    },
    {
        "ситуация": "401",
        "класс": "истёк доступ / нужен токен",
        "повторять": "нет",
        "действие": "однократное обновление токена, затем фиксация инцидента",
    },
    {
        "ситуация": "403",
        "класс": "доступ запрещён / возможная блокировка",
        "повторять": "нет",
        "действие": "остановить домен, снять артефакты, разобраться",
    },
    {
        "ситуация": "400, 404, 409, 422",
        "класс": "ошибка клиента",
        "повторять": "нет",
        "действие": "исправить запрос или пропустить URL",
    },
    {
        "ситуация": "прочие 4xx",
        "класс": "ошибка клиента",
        "повторять": "нет",
        "действие": "не повторять",
    },
]


def classify(status=None, exc: Exception | None = None) -> tuple[str, bool]:
    """Возвращает (класс ошибки, повторять ли)."""
    if exc is not None:
        return "временная сетевая ошибка", True
    if status is None:
        return "неизвестно", False
    if 200 <= status < 300:
        return "успех", False
    if status == 304:
        return "не изменилось (кэш)", False
    if status == 429:
        return "ограничение скорости", True
    if status in (500, 502, 503, 504):
        return "сбой сервера", True
    if status == 401:
        return "истёк доступ / нужен токен", False
    if status == 403:
        return "доступ запрещён / возможная блокировка", False
    if 400 <= status < 500:
        return "ошибка клиента", False
    return "прочее", False


def parse_retry_after(value, now=None) -> float | None:
    """Retry-After в двух форматах: число секунд или HTTP-дата (RFC 9110).

    Возвращает задержку в секундах или None, если заголовок разобрать не удалось.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def backoff_delay(attempt: int, base=0.5, cap=20.0, mode="full_jitter") -> float:
    """Экспоненциальная задержка. По умолчанию — full jitter (рекомендация AWS).

    `attempt` считается с нуля: 0 — первая неудача.
    """
    raw = min(cap, base * 2**attempt)
    if mode == "full_jitter":
        return random.uniform(0, raw)
    if mode == "equal_jitter":
        return raw / 2 + random.uniform(0, raw / 2)
    return raw


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base: float = 0.5
    cap: float = 20.0
    budget_s: float = 45.0  # общий бюджет времени на один URL
    timeout: float = 15.0


def fetch_with_retry(
    session,
    url,
    policy: RetryPolicy,
    logger,
    limiter=None,
    headers=None,
    on_unauthorized=None,
) -> tuple[object | None, list[dict]]:
    """Одна попытка за попыткой: лимит скорости → запрос → классификация → задержка.

    Возвращает (ответ или None, журнал попыток). Каждая попытка пишется в структурный
    журнал: номер, код, класс ошибки, выбранная задержка и её источник.
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    started = time.monotonic()
    attempts: list[dict] = []
    refreshed_token = False
    last = None

    for n in range(1, policy.max_attempts + 1):
        if limiter is not None:
            limiter.acquire(host)
        t0 = time.perf_counter()
        try:
            resp = session.get(url, timeout=policy.timeout, headers=headers)
            error = None
        except Exception as exc:  # requests.RequestException и сетевые сбои
            resp, error = None, f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - t0
        status = resp.status_code if resp is not None else None
        cls, retryable = classify(status, exc=exc_error(error))

        logger.log(
            "request",
            host=host,
            url=url,
            status=status,
            ms=round(latency * 1000),
            attempt=n,
            bytes=len(resp.content) if resp is not None else 0,
            error=error,
        )
        if limiter is not None:
            limiter.observe(host, latency, status)

        # 401: одна попытка обновить токен, дальше — фиксация без повторов
        if status == 401 and not refreshed_token and on_unauthorized is not None:
            refreshed_token = True
            logger.log(
                "auth", host=host, url=url, status=status, action="обновление токена"
            )
            on_unauthorized()
            attempts.append(
                {
                    "попытка": n,
                    "код": status,
                    "класс": cls,
                    "источник задержки": "нет",
                    "ждём, с": 0.0,
                }
            )
            continue

        row = {"попытка": n, "код": status, "класс": cls}

        if resp is not None and not retryable:
            row.update({"источник задержки": "нет", "ждём, с": 0.0})
            attempts.append(row)
            return resp, attempts

        if not retryable:  # исчерпали попытки на невосстановимой ошибке
            row.update({"источник задержки": "нет", "ждём, с": 0.0})
            attempts.append(row)
            return resp, attempts

        ra = (
            parse_retry_after(resp.headers.get("Retry-After"))
            if resp is not None
            else None
        )
        wait = ra if ra is not None else backoff_delay(n - 1, policy.base, policy.cap)
        source = "Retry-After" if ra is not None else "backoff (full jitter)"

        elapsed = time.monotonic() - started
        if elapsed + wait > policy.budget_s:
            row.update(
                {"источник задержки": f"бюджет исчерпан ({source})", "ждём, с": 0.0}
            )
            attempts.append(row)
            logger.log(
                "retry",
                host=host,
                url=url,
                status=status,
                attempt=n,
                error="budget_exhausted",
                delay_s=0.0,
                source=source,
            )
            return resp, attempts

        row.update({"источник задержки": source, "ждём, с": round(wait, 2)})
        attempts.append(row)
        logger.log(
            "retry",
            host=host,
            url=url,
            status=status,
            attempt=n,
            delay_s=round(wait, 2),
            source=source,
        )
        last = resp
        if n < policy.max_attempts:
            time.sleep(wait)  # пауза вежливости, длина вычислена, не фиксирована

    return last, attempts


def exc_error(error_text):
    """Помощник: строковая ошибка → исключение для классификатора."""
    return RuntimeError(error_text) if error_text else None
