# Лаб. 7 — вариант 6: многодоменный сбор

## Запуск
```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m pip install -r 7lab\requirements.txt
cd 7lab
..\.venv\Scripts\python.exe -m src.collect --profile A --run-id A      # наивный
..\.venv\Scripts\python.exe -m src.collect --profile B --run-id B      # вежливый
..\.venv\Scripts\python.exe -m src.collect --profile C --run-id C      # + условные, gzip
..\.venv\Scripts\python.exe -m src.collect --profile C --run-id C_incr --incremental
```
Коды возврата: 0 — зелёный/жёлтый, 2 — красный, 3 — инцидент блокировки.

Распределённый режим: `--workers 4` (закрепление домена), `--no-pinning` (антипаттерн).

## Структура
- `src/` — `limiter.py`, `retry.py`, `breaker.py`, `detector.py`, `observability.py`,
  `faultstub.py`, `collect.py`.
- `logs/` — JSONL-журналы прогонов; `logs/incidents/` — артефакты инцидентов.
- `reports/` — отчёты прогонов, `comparison.md`, `mechanisms.md`.
- `state/` — ETag и водяной знак.
- `analysis.ipynb` — полный разбор с графиками (перезапуск: `Run All` в kernel `parsing-venv`).
- `nocode/` — `workflow.json`, `nocode_notes.md`, скриншоты.
- `report.md`, `legal_checklist.md`.

## Примечания
- Отчёт о запуске `_gen_nb.py` генерирует блокнот; исполнение очищает `logs/run_*` и отчёты.
- Порт 8765 занимает заглушка; прогоны поднимают её сами.
