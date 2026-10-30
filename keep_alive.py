# keep_alive.py — мини HTTP-сервер для хостинга (Render).
#
# Зачем: на Render бот запускается как «Web Service», и Render требует,
# чтобы приложение слушало порт из переменной окружения PORT. Этот сервер
# отвечает «200 OK» на любой запрос — этого достаточно, чтобы Render
# считал сервис живым.
#
# Бонус: бот сам «будит» себя каждые 10 минут (см. _self_ping в bot.py),
# поэтому бесплатный тариф Render не усыпляет бота после 15 минут тишины.
# На Windows/macOS этот модуль ничего не делает (переменной PORT нет).
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

log = logging.getLogger("keepalive")


class _Handler(BaseHTTPRequestHandler):
    def _ok(self):
        body = b"OK"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok()

    def log_message(self, fmt, *args):
        pass  # не засоряем логи бота


def start(port: int) -> None:
    """Поднять HTTP-сервер в фоновом потоке (ошибка порта не роняет бота)."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        Thread(target=server.serve_forever, daemon=True,
               name="keepalive-http").start()
        log.info("HTTP keep-alive сервер на порту %s", port)
    except Exception as e:
        log.critical("Не удалось занять порт %s: %s", port, e)
