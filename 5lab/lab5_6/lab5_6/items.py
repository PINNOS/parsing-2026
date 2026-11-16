"""Схема записи (вариант 6): два типа элементов под два источника.

Каждый Item описывает ровно одну строку итоговой БД: books — книга
(бизнес-ключ UPC), quotes — цитата (бизнес-ключ пара автор+цитата).
Поля помечены звёздочкой — обязательные, проверяются в ValidationPipeline.
"""

import scrapy


class BookItem(scrapy.Item):
    # уровень 1 (список)
    title = scrapy.Field()  # название, *.get() + проверка
    list_price = scrapy.Field()  # цена из списка (для сверки с карточкой)
    rating = scrapy.Field()  # 1..5, из класса star-rating
    list_url = scrapy.Field()  # URL карточки, куда ушёл запрос уровня 2
    # уровень 2 (карточка)
    upc = scrapy.Field()  # *. бизнес-ключ
    price_excl_tax = scrapy.Field()  # float после очистки
    price_incl_tax = scrapy.Field()  # float после очистки
    tax = scrapy.Field()  # float
    availability = scrapy.Field()  # исходная строка "In stock (20 available)"
    quantity = scrapy.Field()  # int, вытащено из availability
    num_reviews = scrapy.Field()  # int
    category = scrapy.Field()  # из хлебных крошек карточки
    description = scrapy.Field()  # может отсутствовать -> None
    url = scrapy.Field()  # URL карточки
    source = scrapy.Field()  # "books"


class QuoteItem(scrapy.Item):
    # уровень 1 (список цитат)
    quote = scrapy.Field()  # *. текст цитаты
    author = scrapy.Field()  # *. автор
    author_url = scrapy.Field()  # страница автора (уровень 2)
    tags = scrapy.Field()  # список тегов
    # уровень 2 (страница автора)
    born_date = scrapy.Field()  # ISO-8601 после очистки
    born_date_raw = scrapy.Field()  # исходная "September 19, 1948"
    born_location = scrapy.Field()  # "in Berwyn, Illinois"
    biography = scrapy.Field()  # может отсутствовать -> None
    source = scrapy.Field()  # "quotes"
