"""Настройки Scrapy-проекта лабораторной работы 5 (вариант 6).

Каждый ключ сопровождается обоснованием — таблица настроек вынесена в отчёт.
"""

BOT_NAME = "lab5_6"

SPIDER_MODULES = ["lab5_6.spiders"]
NEWSPIDER_MODULE = "lab5_6.spiders"

# --- Вежливость -----------------------------------------------------------
# Обязательное требование: читаем robots.txt тренировочной площадки.
ROBOTSTXT_OBEY = True

# Честный учебный User-Agent с указанием назначения и контакта.
USER_AGENT = "StudentScraperBot/1.0 (+https://github.com/PINNOS/parsing; educational data-harvesting course)"

# Общий параллелизм; на домен — меньше, чтобы не создавать поток запросов
# к одному серверу (см. следующий параметр).
CONCURRENT_REQUESTS = 8
# Ограничение на домен <= 4 — требование задания.
CONCURRENT_REQUESTS_PER_DOMAIN = 4

# Пауза между запросами к одному домену. При включённом AutoThrottle Scrapy
# сам будет держать целевую нагрузку, задержка служит нижней границей.
DOWNLOAD_DELAY = 0.25
# Разброс задержки +/-50% (равномерный) вместо фиксированного интервала.
DOWNLOAD_DELAY_JITTER = 0.5

# --- AutoThrottle ---------------------------------------------------------
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 0.5
AUTOTHROTTLE_MAX_DELAY = 10.0
# Целевая конкурентность на домен: ~2 запроса одновременно — вежливая
# нагрузка на тренировочный сервер при CONCURRENT_REQUESTS_PER_DOMAIN = 4.
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0
AUTOTHROTTLE_DEBUG = False

# --- Предохранители и повторы --------------------------------------------
DOWNLOAD_TIMEOUT = 30
# Повторы только для 429 и 5xx (плюс таймаут 408) — 4xx не повторяем.
RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [429, 500, 502, 503, 504, 522, 524, 408]

# Два уровня обхода: список -> карточка/страница автора. Глубина при этом
# растёт вдоль цепочки пагинации (страница N списка имеет глубину N),
# поэтому лимит — потолок на весь обход (каталог books — 50 страниц +
# карточка): жёсткий предохранитель от зацикливания ссылок.
DEPTH_LIMIT = 60

# Предохранитель на объём: итоговый прогон останавливается на 350 записях
# (задание требует >= 300); значение фиксируется в отчёте.
CLOSESPIDER_ITEMCOUNT = 350

# --- Возобновляемость и кэш ----------------------------------------------
# JOBDIR задаётся при запуске (демонстрация в ноутбуке):
#   scrapy crawl books -s JOBDIR=jobstate/books_run1
# Каталог состояния хранит очереди и душефильтр между запусками.

# HTTP-cache для отладки: в проде выключен, включения — точечные (-s).
HTTPCACHE_ENABLED = False
HTTPCACHE_DIR = "httpcache"
HTTPCACHE_IGNORE_HTTP_CODES = []  # кэшируем и ошибочные ответы — для отладки
HTTPCACHE_EXPIRATION_SECS = 0  # записи не устаревают

# --- Логи и выгрузки ------------------------------------------------------
LOG_LEVEL = "INFO"
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"

# Требование задания: схема записи описана в items.py (scrapy.Item),
# вся обработка — в pipelines.py, логика обхода — в файлах пауков.
ITEM_PIPELINES = {
    "lab5_6.pipelines.ValidatePipeline": 100,
    "lab5_6.pipelines.CleanPipeline": 200,
    "lab5_6.pipelines.DedupPipeline": 300,
    "lab5_6.pipelines.SQLitePipeline": 400,
}

# Отпечаток запросов в 2.7 не влияет на кэш и душефильтр для статических
# страниц; константа убирает предупреждения о скорости.
