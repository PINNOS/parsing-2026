# Лаб. 4, вариант 6: сбор с динамической страницы (Playwright + дешёвая альтернатива + no-code)

Площадки: `scrapingcourse.com/button-click` (динамика, «Load more») и `books.toscrape.com` (статика, пагинация).
Отчёт: `report.md`. Исходное условие: `task_4lab.md`.

## Воспроизведение с нуля (Windows, PowerShell)

Из корня репозитория `parsing/`:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r 4lab\requirements.txt
python -m playwright install chromium

python 4lab\collect_browser.py                 # браузерный сбор -> data_browser.csv, collection_log.csv   (~30-60 с)
python 4lab\collect_browser.py --block-media   # замер с блокировкой медиа -> run_summary_blocked.json       (~30-60 с)
python 4lab\collect_light.py                   # requests + XHR -> data_light.csv, сверка                   (~25-45 с)
python 4lab\smoke_check.py                     # smoke-check и качество -> quality_report.json
```

Ожидаемый результат: 227 уникальных записей в каждом наборе (147 `load_more` + 80 `books`), сверка `matched_by_business_key = 227`, `name/price/url mismatch = 0`.

Коды возврата:
- `collect_browser.py`: `0` — ок; `2` — сбой ожидания (скриншот и HTML в `artifacts/`); `3` — записей меньше `--target` (200).
- `smoke_check.py`: `1` — красный вердикт (пустые обязательные поля > 5 % или дубликаты по бизнес-ключу).

## Состав

| Файл | Назначение |
|---|---|
| `collect_browser.py` | Playwright (sync): цикл «Load more» и пагинация «next», предохранители, журнал, замер |
| `collect_light.py` | Дешёвая альтернатива: XHR `GET /ajax/products?offset=N` и статическая пагинация через requests |
| `smoke_check.py` | Полнота обязательных полей, дубликаты по бизнес-ключу, типизация цены |
| `data_browser.csv` / `data_light.csv` | Наборы (227 строк каждый) |
| `collection_log.csv` | Журнал циклов браузерного сбора |
| `reconcile_light_vs_browser.json` | Сверка двух наборов |
| `run_summary_*.json` | Метрики прогонов (время, запросы, трафик, память) |
| `quality_report.json` | Результат smoke-check |
| `logs/run_*.jsonl` | Структурные журналы (JSON Lines) |
| `artifacts/` | Скриншот и HTML при сбое ожидания (сейчас пусто — сбоев в финальных прогонах не было) |
| `nocode/workflow.json` | n8n-сценарий (импортирован и прогнан, 147 записей; см. отчёт, раздел 7) |
| `nocode/nocode_result.csv` | Результат прогона n8n (147 строк) |

## Бизнес-ключ

Дедупликация и сверка идут по нормализованному названию товара (`[^a-z0-9]` удалено, нижний регистр), а не по URL: одни и те же товары встречаются с разными URL (завершающий `/`, двойные дефисы, закодированные символы).

## Ограничения запуска

- Браузерный сбор: 1 с паузы между циклами (вежливость). Время прогона в основном определяется этой паузой.
- `--block-media` меняет только трафик и память, набор данных тот же.
- Сайты тренировочные. Раздел `scrapingcourse.com/ecommerce/` **не используется**: в `robots.txt` стоит `Disallow: /ecommerce/*`.
