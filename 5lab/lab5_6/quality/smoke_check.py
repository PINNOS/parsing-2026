"""Smoke-check деградации между прогонами.

Предупреждение (exit code 1), если:
  - число записей текущего прогона меньше предыдущего более чем на 20 %;
  - доля пустых обязательных полей в БД превышает 5 %.

Запуск из корня проекта:
  python quality/smoke_check.py data/stats_c1_quotes_cache_store.json data/stats_c2_quotes_cache_hit.json
"""

import json
import sqlite3
import sys
from pathlib import Path

DB = Path("data/crawl.db")
DROP_LIMIT = 0.20
EMPTY_LIMIT = 0.05
REQUIRED = {
    "books": ["upc", "title", "price_excl_tax", "availability", "category"],
    "quotes": ["quote", "author", "author_url"],
}


def stored(path: Path) -> int:
    return json.loads(path.read_text(encoding="utf-8"))["stats"].get("stored/rows", 0)


def empty_shares(conn: sqlite3.Connection) -> dict:
    out = {}
    for table, cols in REQUIRED.items():
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for col in cols:
            empty = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IS NULL OR {col} = ''"
            ).fetchone()[0]
            out[f"{table}.{col}"] = empty / total if total else 1.0
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: smoke_check.py <current_stats.json> <previous_stats.json>")
        return 2
    cur, prev = Path(sys.argv[1]), Path(sys.argv[2])
    warnings = []

    cur_n, prev_n = stored(cur), stored(prev)
    drop = (prev_n - cur_n) / prev_n if prev_n else 0.0
    print(f"записей: текущий {cur_n}, предыдущий {prev_n}, падение {drop:.1%}")
    if drop > DROP_LIMIT:
        warnings.append(f"число записей упало на {drop:.1%} (порог {DROP_LIMIT:.0%})")

    with sqlite3.connect(DB) as conn:
        for name, share in empty_shares(conn).items():
            flag = " <- превышение" if share > EMPTY_LIMIT else ""
            print(f"пусто {name}: {share:.1%}{flag}")
            if share > EMPTY_LIMIT:
                warnings.append(
                    f"доля пустых {name} = {share:.1%} (порог {EMPTY_LIMIT:.0%})"
                )

    if warnings:
        print("ПРЕДУПРЕЖДЕНИЕ:")
        for w in warnings:
            print(" -", w)
        return 1
    print("OK: деградации не обнаружено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
