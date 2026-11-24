"""Лаб. 7, вариант 6. Управление скоростью: token bucket, джиттер, адаптивная задержка
и лимиты ПО ДОМЕНАМ (структура «домен → ограничитель»), а не глобально.

Здесь же читается robots.txt: `Crawl-delay` имеет приоритет над нашими настройками,
поэтому разбор robots.txt — часть расчёта скорости, а не отдельная формальность.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


@dataclass
class DomainLimits:
    """Параметры ведра токенов для одного домена."""

    rate: float  # запросов в секунду
    burst: int = 1  # допустимый всплеск, запросов
    jitter: float = 0.25  # доля случайного разброса интервала (20–30% по заданию)


class TokenBucket:
    """Средняя скорость не выше `rate` запросов в секунду, короткий всплеск — до `burst`."""

    def __init__(self, rate, burst=1, jitter=0.25, clock=time.monotonic):
        self.rate = float(rate)
        self.burst = max(1, int(burst))
        self.jitter = jitter
        self._clock = clock
        self.tokens = float(self.burst)
        self.updated = self._clock()
        self.waited = 0.0

    def acquire(self) -> float:
        """Забирает токен, при необходимости ждёт. Возвращает суммарное ожидание, с.

        `time.sleep` здесь — только пауза вежливости вычисленной длительности,
        а не ожидание данных: цикл ограничен доступностью токена.
        """
        slept = 0.0
        while True:
            now = self._clock()
            self.tokens = min(
                self.burst, self.tokens + (now - self.updated) * self.rate
            )
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                if self.jitter:
                    nap = random.uniform(0, self.jitter / self.rate)
                    time.sleep(nap)
                    slept += nap
                self.waited += slept
                return slept
            nap = (1 - self.tokens) / self.rate
            time.sleep(nap)
            slept += nap


class AdaptiveDelay:
    """Чем медленнее отвечает сервер, тем реже мы его трогаем.

    Логика AutoThrottle (занятие 5): целевая задержка ≈ латентность / целевая параллельность.
    Ответ 429/503 — прямая просьба сбавить скорость, задержка удваивается.
    """

    def __init__(
        self, target_concurrency=1.0, start=0.0, min_delay=0.0, max_delay=10.0
    ):
        self.target = target_concurrency
        self.delay = start
        self.min_delay, self.max_delay = min_delay, max_delay
        self.history = []

    def observe(self, latency_s: float, status) -> float:
        target = latency_s / self.target
        self.delay = (self.delay + target) / 2.0  # сглаживание
        if status in (429, 503):
            self.delay = min(self.max_delay, max(self.delay * 2, 1.0))
        self.delay = min(self.max_delay, max(self.min_delay, self.delay))
        self.history.append(round(self.delay, 3))
        return self.delay


@dataclass
class DomainLimiters:
    """Структура «домен → ограничитель»: у каждого домена своё ведро и свой лимит."""

    limits: dict
    adaptive: "AdaptiveDelay | None" = None
    buckets: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    def limits_for(self, host: str) -> DomainLimits:
        """Незнакомый домен получает самый осторожный из настроенных лимитов."""
        if host in self.limits:
            return self.limits[host]
        return DomainLimits(rate=min(l.rate for l in self.limits.values()))

    def bucket(self, host: str) -> TokenBucket:
        if host not in self.buckets:
            lim = self.limits_for(host)
            self.buckets[host] = TokenBucket(lim.rate, lim.burst, lim.jitter)
        return self.buckets[host]

    def acquire(self, host: str) -> dict:
        """Ждёт разрешения на запрос к домену и задержку по его латентности."""
        bucket_wait = self.bucket(host).acquire()
        adaptive_wait = 0.0
        if self.adaptive and self.adaptive.delay > 0:
            adaptive_wait = self.adaptive.delay
            time.sleep(adaptive_wait)
        st = self.stats.setdefault(
            host, {"acquires": 0, "bucket_wait_s": 0.0, "adaptive_wait_s": 0.0}
        )
        st["acquires"] += 1
        st["bucket_wait_s"] = round(st["bucket_wait_s"] + bucket_wait, 3)
        st["adaptive_wait_s"] = round(st["adaptive_wait_s"] + adaptive_wait, 3)
        return {
            "bucket_wait_s": round(bucket_wait, 3),
            "adaptive_wait_s": round(adaptive_wait, 3),
        }

    def observe(self, host: str, latency_s: float, status) -> float:
        return self.adaptive.observe(latency_s, status) if self.adaptive else 0.0

    def regulation(self, host: str) -> float:
        """Фактически выдерживаемый интервал: ведро + адаптивная задержка."""
        return 1.0 / self.limits_for(host).rate + (
            self.adaptive.delay if self.adaptive else 0.0
        )

    def table(self) -> list[dict]:
        rows = []
        for host, lim in self.limits.items():
            st = self.stats.get(host, {})
            rows.append(
                {
                    "домен": host,
                    "лимит, запр/с": lim.rate,
                    "всплеск": lim.burst,
                    "джиттер": f"{lim.jitter:.0%}",
                    "запросов": st.get("acquires", 0),
                    "ожидание в ведре, с": round(st.get("bucket_wait_s", 0.0), 3),
                    "адаптивная задержка, с": round(st.get("adaptive_wait_s", 0.0), 3),
                }
            )
        return rows


def read_robots(session, base: str, user_agent: str, probe_paths=("/",)) -> dict:
    """Практический отчёт по robots.txt: can_fetch, Crawl-delay, request-rate, Sitemap.

    404 трактуется как «явных правил нет» (файл parse([])). Это НЕ означает «можно всё»:
    остаются условия использования и требование не мешать сайту.
    """
    rp = RobotFileParser()
    robots_url = base.rstrip("/") + "/robots.txt"
    raw, status, error = None, None, None
    try:
        resp = session.get(robots_url, timeout=15)
        status = rsp_status = session and resp.status_code
        if resp.status_code == 200:
            raw = resp.text
            rp.parse(raw.splitlines())
        else:
            rp.parse([])  # 404: явных правил нет
    except Exception as exc:  # сетевая ошибка не должна ронять аудит
        rp.parse([])
        status, error = None, type(exc).__name__
    _ = status
    return {
        "домен": urlparse(base).netloc,
        "robots.txt": "есть" if raw else "нет (404)",
        "строк": len(raw.splitlines()) if raw else 0,
        "Crawl-delay для нас": rp.crawl_delay(user_agent),
        "request_rate": rp.request_rate(user_agent),
        "карт сайта": len(rp.site_maps() or []),
        "sitemap": (rp.site_maps() or [None])[0],
        **{
            f"can_fetch {p}": rp.can_fetch(user_agent, base.rstrip("/") + p)
            for p in probe_paths
        },
    }


def plan_domain_workers(hosts, workers: int) -> list[dict]:
    """Распределённый случай: «один воркер на домен».

    Четыре процесса с общим лимитом 2 запр/с создают на сайте 8 запр/с. Поэтому домен
    закрепляется ровно за одним воркером: суммарная скорость на домен равна объявленному
    лимиту, а не умножается на число процессов. Внутри одного процесса домены обрабатывает
    столько же последовательных очередей, поэтому общий счётчик не нужен.
    """
    hosts = list(hosts)
    workers = max(1, int(workers))
    assignment = []
    for i, host in enumerate(hosts):
        assignment.append({"worker": i % workers, "host": host})
    return assignment
