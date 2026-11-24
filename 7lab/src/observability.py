"""Лаб. 7, вариант 6. Наблюдаемость: структурные логи и отчёт о прогоне.

Один лог-файл — JSON Lines: каждая строка содержит обязательные поля
`ts`, `run_id`, `event`, `host`, `url`, `status`, `ms`, `attempt`, `bytes`, `error`.
Текстовых логов в проекте нет: из них невозможно считать метрики.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

# Пороговые значения отчёта о прогоне (пункт 4.6.3 задания).
THRESHOLDS = [
    {
        "metric": "error_rate_pct",
        "op": ">",
        "limit": 5.0,
        "level": "warn",
        "why": "выше 5% ошибок прогон перестаёт быть воспроизводимым: результат "
        "зависит от удачи, а не от логики сборщика",
    },
    {
        "metric": "rate_limited_pct",
        "op": ">",
        "limit": 1.0,
        "level": "warn",
        "why": "429/403 означают, что заявленная скорость выше допустимой для домена; "
        "сигнал снизить лимит до исчезновения ответов",
    },
    {
        "metric": "latency_p95_ms",
        "op": ">",
        "limit": 3000.0,
        "level": "warn",
        "why": "p95 выше 3 с означает перегрузку источника; токен-бакет обязан отступить "
        "через адаптивную задержку",
    },
    {
        "metric": "breaker_activations",
        "op": ">=",
        "limit": 1,
        "level": "warn",
        "why": "срабатывание предохранителя — инцидент: домен изолирован, нужен разбор причин",
    },
    {
        "metric": "dropped_pct",
        "op": ">",
        "limit": 10.0,
        "level": "warn",
        "why": "более 10% отброшенных записей чаще всего означают поломку селекторов "
        "или смену вёрстки",
    },
    {
        "metric": "block_incidents",
        "op": ">=",
        "limit": 1,
        "level": "critical",
        "why": "вердикт «блокировка» — остановка домена и ненулевой код возврата: "
        "сбор прекращается до разбора причин",
    },
    {
        "metric": "records_vs_median_pct",
        "op": "<",
        "limit": 80.0,
        "level": "critical",
        "why": "падение объёма ниже 80% медианы прошлых прогонов — признак «тихой» деградации "
        "сбора или поломки селекторов",
    },
    {
        "metric": "avg_size_vs_median_pct",
        "op": "<",
        "limit": 70.0,
        "level": "critical",
        "why": "средний размер ответа ниже 70% медианы истории — вероятна заглушка "
        "или урезанная страница при коде 200",
    },
]


class RunLogger:
    """Структурный журнал: одна строка — один JSON-объект, никакого текста."""

    REQUEST_FIELDS = ("host", "url", "status", "ms", "attempt", "bytes", "error")

    def __init__(self, path, run_id: str, **meta):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        if meta:
            self.log("run_start", **meta)

    def log(self, event: str, **fields) -> dict:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "event": event,
        }
        for key in self.REQUEST_FIELDS:
            if key in fields:
                rec[key] = fields.pop(key)
        rec.update(fields)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


def load_log(path):
    """Читает журнал в DataFrame: каждая строка — отдельное событие."""
    import pandas as pd

    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return pd.DataFrame(rows)


_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def metrics_from_log(path, extras: dict | None = None) -> dict:
    """Считает метрики прогона из структурного журнала.

    `extras` — то, чего в журнале нет: собранные и отброшенные записи, длительность,
    сравнение с историей прогонов, выбранные лимиты.
    """
    df = load_log(path)
    req = df[df["event"] == "request"].copy() if len(df) else df
    n = len(req)
    status = req["status"] if n else None
    lat = req["ms"].dropna() if n else None
    m = {
        "requests": n,
        "hosts": int(req["host"].nunique()) if n else 0,
        "status_counts": (
            {
                int(k): int(v)
                for k, v in status.fillna(-1).astype(int).value_counts().items()
            }
            if n
            else {}
        ),
        "error_rate_pct": (
            round(float((status.isna() | (status >= 400)).mean() * 100), 2)
            if n
            else 0.0
        ),
        "rate_limited_pct": (
            round(float(status.isin([403, 429]).mean() * 100), 2) if n else 0.0
        ),
        "retries": int((req["attempt"].fillna(1) > 1).sum()) if n else 0,
        "latency_median_ms": int(lat.median()) if n and len(lat) else 0,
        "latency_p95_ms": int(lat.quantile(0.95)) if n and len(lat) else 0,
        "traffic_bytes": int(req["bytes"].fillna(0).sum()) if n else 0,
        "cached_304": int((status == 304).sum()) if n else 0,
        "breaker_activations": int((df["event"] == "breaker_open").sum())
        if len(df)
        else 0,
    }
    # счётчики, которых нет в журнале
    defaults = {"dropped_pct": 0.0, "collected": 0, "dropped": 0, "duration_s": 0.0}
    m.update(defaults)
    if extras:
        m.update(extras)
    if m.get("history_comparison") is False:
        # Инкрементальный прогон не загружает данные повторно: сравнивать его объём
        # и средний размер ответа с историей полных прогонов нельзя.
        m["records_vs_median_pct"] = None
        m["avg_size_vs_median_pct"] = None
    return m


def evaluate(metrics: dict) -> tuple[list[dict], str]:
    """Применяет пороги: возвращает (сработавшие оповещения, итоговый статус).

    «зелёный» — оповещений нет, «жёлтый» — только warn, «красный» — есть critical.
    """
    alerts = []
    for rule in THRESHOLDS:
        value = metrics.get(rule["metric"])
        if value is None:
            continue
        hit = {
            ">": value > rule["limit"],
            ">=": value >= rule["limit"],
            "<": value < rule["limit"],
            "<=": value <= rule["limit"],
        }[rule["op"]]
        if hit:
            alerts.append({**rule, "value": value})
    if any(a["level"] == "critical" for a in alerts):
        return alerts, "красный"
    return alerts, "жёлтый" if alerts else "зелёный"


def alerts_text(alerts) -> str:
    if not alerts:
        return "оповещений нет"
    return "; ".join(
        f"{a['metric']} {a['op']} {a['limit']} (факт {a['value']}) [{a['level']}]"
        for a in alerts
    )


def write_report(path, metrics: dict, alerts: list, status: str) -> dict:
    report = {
        "metrics": metrics,
        "alerts": alerts,
        "status": status,
        "thresholds": THRESHOLDS,
    }
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def latency_stats(ms_values) -> dict:
    """Медиана и p95 без зависимости от pandas — для разделов блокнота."""
    vals = sorted(v for v in ms_values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "p95": None, "mean": None}
    k = max(0, min(len(vals) - 1, int(round(0.95 * (len(vals) - 1)))))
    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "p95": vals[k],
        "mean": round(statistics.fmean(vals), 1),
    }
