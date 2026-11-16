"""Smoke-check и проверка качества: полнота ключевых полей, дубликаты, типизация.

Предупреждение печатается, если доля пустых обязательных полей > порога (по умолчанию 5%).
Код возврата 1 при красном вердикте (есть пустые обязательные поля или дубли по ключу).
"""

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED = ["name", "price", "url"]
THRESHOLD = 0.05


def load(path):
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def check(rows, label):
    n = len(rows)
    result = {"dataset": label, "records": n, "fields": {}, "warnings": []}
    for col in REQUIRED:
        empty = sum(1 for r in rows if not (r.get(col) or "").strip())
        share = empty / n if n else 1.0
        result["fields"][col] = {"empty": empty, "empty_share": round(share, 4)}
        if share > THRESHOLD:
            result["warnings"].append(f"{col}: пустых {share:.1%} > {THRESHOLD:.0%}")
    keys = [re.sub(r"[^a-z0-9]", "", (r["name"] or "").lower()) for r in rows]
    dup_keys = len(keys) - len(set(keys))
    result["duplicate_keys_by_name"] = dup_keys
    if dup_keys:
        result["warnings"].append(f"дубликаты по бизнес-ключу (название): {dup_keys}")
    bad_price = [
        r["price"]
        for r in rows
        if r.get("price") and not re.fullmatch(r"\d+(\.\d+)?", r["price"])
    ]
    result["price_not_numeric"] = len(bad_price)
    if bad_price:
        result["warnings"].append(f"цена не число: {len(bad_price)}")
    result["verdict"] = "red" if result["warnings"] else "green"
    return result


def main():
    results = []
    for name in ("data_browser.csv", "data_light.csv"):
        path = HERE / name
        if not path.exists():
            print(f"[WARN] нет файла {name}")
            continue
        res = check(load(path), name)
        results.append(res)
        for w in res["warnings"]:
            print(f"[WARN] {name}: {w}")
        print(
            f"{name}: записей={res['records']} verdict={res['verdict']} "
            f"дубли по бизнес-ключу={res['duplicate_keys_by_name']}"
        )
    (HERE / "quality_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 1 if any(r["verdict"] == "red" for r in results) or not results else 0


if __name__ == "__main__":
    sys.exit(main())
