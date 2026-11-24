"""Лаб. 7, вариант 6. Детектор блокировок и деградации.

Проверяется не только код ответа (за «проверку только кода» штраф −7), но и содержимое:
текстовые маркеры заглушек, аномально малый размер ответа, отсутствие ожидаемых элементов
при коде 200. Вердикт: `ok` / `подозрение` / `блокировка`.

Дополнительно — сравнение прогона с историей: объём записей и средний размер ответа
сопоставляются с медианой прошлых прогонов; отклонение сверх порога (20% и 30%) даёт «красный».
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Маркеры заглушек и страниц-проверок (англ. и рус.). Поиск в тексте в нижнем регистре.
BLOCK_MARKERS = [
    "captcha",
    "are you human",
    "are you a robot",
    "access denied",
    "access to this page has been denied",
    "unusual traffic",
    "too many requests",
    "rate limit exceeded",
    "request blocked",
    "cf-browser-verification",
    "checking your browser",
    "enable javascript and cookies",
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "слишком много запросов",
    "введите капчу",
]

# Пороги сравнения с историей прогонов (пункт 4.5.3): падение объёма > 20%,
# падение среднего размера ответа > 30%.
HISTORY_RULES = {
    "records": {"min_pct": 80.0, "label": "записей к медиане"},
    "avg_bytes": {"min_pct": 70.0, "label": "средний размер ответа к медиане"},
}


def inspect_response(
    resp, expected_selector_count=None, min_bytes=1000
) -> tuple[str, list]:
    """Вердикт по одному ответу: `ok` / `подозрение` / `блокировка` и список сигналов."""
    text = getattr(resp, "text", "") or ""
    body = text.lower()
    found = sorted({m for m in BLOCK_MARKERS if m in body})
    status = getattr(resp, "status_code", None)
    size = len(getattr(resp, "content", b"") or b"")
    signals = []
    if status == 403:
        signals.append("код 403 — доступ запрещён")
    if status == 429:
        signals.append("код 429 — ограничение скорости")
    if status == 401:
        signals.append("код 401 — требуется авторизация")
    if found:
        signals.append("маркеры: " + ", ".join(found))
    if status == 200 and size and size < min_bytes:
        signals.append(f"аномально малый ответ: {size} байт < {min_bytes}")
    if status == 200 and expected_selector_count == 0:
        signals.append("при коде 200 нет ожидаемых элементов")
    # «Блокировка» — это 403 и заглушка, отданная с кодом 200 (тихая подмена).
    # 429 остаётся «подозрением», даже если в теле написано Too many requests:
    # это ограничение скорости, которое разбирает слой повторов, а не блокировка.
    silent_stub = status == 200 and bool(found)
    if silent_stub or status == 403:
        return "блокировка", signals
    return ("подозрение", signals) if signals else ("ok", signals)


def save_incident(incidents_dir, name: str, resp, meta: dict | None = None) -> dict:
    """Артефакты подозрительного ответа: тело, заголовки, метаданные.

    Для браузерного сбора здесь же сохранялся бы скриншот; в этом варианте сбор
    HTTP-овый, поэтому сохраняются тело и заголовки.
    """
    out_dir = Path(incidents_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    base = out_dir / f"{stamp}_{name}"
    info = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": getattr(resp, "url", None),
        "status": getattr(resp, "status_code", None),
        "headers": dict(getattr(resp, "headers", {}) or {}),
        "bytes": len(getattr(resp, "content", b"") or b""),
        **(meta or {}),
    }
    base.with_suffix(".html").write_bytes(getattr(resp, "content", b"") or b"")
    base.with_suffix(".json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "saved": True,
        "body": str(base.with_suffix(".html")),
        "meta": str(base.with_suffix(".json")),
    }


def compare_with_history(current: dict, history: list[dict]) -> list[dict]:
    """Сравнивает прогон с медианой истории. Возвращает строки для таблицы.

    `current` и элементы `history`: {"run": str, "records": int, "avg_bytes": float}.
    """
    rows = []
    for key, rule in HISTORY_RULES.items():
        past = sorted(h[key] for h in history if h.get(key))
        median = past[len(past) // 2] if past else None
        value = current.get(key)
        pct = round(value / median * 100, 1) if median and value is not None else None
        rows.append(
            {
                "показатель": rule["label"],
                "текущий прогон": value,
                "медиана истории": median,
                "история, прогонов": len(past),
                "доля, %": pct,
                "порог, %": f"≥ {rule['min_pct']:.0f}",
                "статус": (
                    "—"
                    if pct is None
                    else ("КРАСНЫЙ" if pct < rule["min_pct"] else "ок")
                ),
            }
        )
    return rows


def load_history(path) -> list[dict]:
    """История прогонов: JSON-массив объектов. Файла нет — истории нет."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def append_history(path, entry: dict) -> list[dict]:
    history = load_history(path)
    history = [h for h in history if h.get("run") != entry.get("run")] + [entry]
    Path(path).write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return history
