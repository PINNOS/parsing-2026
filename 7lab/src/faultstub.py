"""Лаб. 7, вариант 6. Локальная заглушка для воспроизведения кодов ответа.

Задание разрешает воспроизводить сценарии отказа на подготовленной заглушке
(пункт 4.5.4) и на httpbin.org. Локальный стенд удобнее: он даёт серию 503,
`Retry-After` в числовом и датном формате, 401, 403, «тихую» заглушку с кодом 200
и медленный ответ для проверки таймаута — и всё это без нагрузки на внешние сайты.

Маршруты:
  /flaky              — первые 3 запроса 503, далее 200 (серия сбоев для предохранителя)
  /ratelimit-numeric  429 + Retry-After: 1        (числовой формат)
  /ratelimit-date     429 + Retry-After: <HTTP-дата +2 с>
  /unauthorized       401
  /forbidden          403
  /stub200            200, но тело — заглушка «Access denied / captcha»
  /slow               200 через 2 с (проверка таймаута)
  /ok                 200, нормальный ответ
"""

from __future__ import annotations

import email.utils
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STUB_BODY = "<html><body><article class='product_pod'>ok</article></body></html>"
STUB_CAPTCHA = (
    "<html><body><h1>Access denied</h1>"
    "<p>Please complete the CAPTCHA to continue. Unusual traffic from your network.</p>"
    "</body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    flaky_calls = 0  # общий счётчик для серии 503

    def log_message(self, *args):  # текстовых логов не пишем
        return

    def _send(self, status: int, body: str, headers: dict | None = None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_ok(self, body: str, etag: str):
        """Обычный ответ с валидатором, но с поддержкой условного запроса (304)."""
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, body, {"ETag": etag})

    def do_GET(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
        path = self.path.split("?")[0]
        if path == "/flaky":
            _Handler.flaky_calls += 1
            if _Handler.flaky_calls <= 3:
                self._send(503, "<html><body>Service Unavailable</body></html>")
            else:
                self._send_ok(STUB_BODY, '"flaky-v1"')
        elif path == "/ratelimit-numeric":
            self._send(
                429, "<html><body>Too many requests</body></html>", {"Retry-After": "1"}
            )
        elif path == "/ratelimit-date":
            when = datetime.now(timezone.utc) + timedelta(seconds=2)
            self._send(
                429,
                "<html><body>Too many requests</body></html>",
                {"Retry-After": email.utils.format_datetime(when)},
            )
        elif path == "/unauthorized":
            self._send(
                401,
                "<html><body>Unauthorized</body></html>",
                {"WWW-Authenticate": 'Bearer realm="lab7"'},
            )
        elif path == "/forbidden":
            self._send(403, "<html><body>Forbidden</body></html>")
        elif path == "/stub200":
            self._send(200, STUB_CAPTCHA)
        elif path == "/slow":
            time.sleep(2)
            self._send_ok(STUB_BODY, '"slow-v1"')
        elif path == "/ok":
            self._send_ok(STUB_BODY, '"ok-v1"')
        else:
            self._send(404, "<html><body>Not found</body></html>")


def start_stub(port: int = 8765) -> ThreadingHTTPServer:
    """Поднимает заглушку в отдельном потоке. Возвращает сервер (нужно .shutdown())."""
    _Handler.flaky_calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_stub(server) -> None:
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    print(
        "Заглушка: http://127.0.0.1:8765/flaky|/ratelimit-numeric|/ratelimit-date|"
        "/unauthorized|/forbidden|/stub200|/slow|/ok"
    )
    _server = ThreadingHTTPServer(("127.0.0.1", 8765), _Handler)
    try:
        _server.serve_forever()  # остановка по Ctrl+C, ограничение снаружи
    except KeyboardInterrupt:
        pass
    finally:
        _server.server_close()
