# tg.py — минимальный асинхронный клиент Telegram Bot API на чистом httpx.
#
# Зачем: aiogram тянет aiohttp и pydantic-core, у которых под некоторые
# версии Python (например, самые свежие) нет готовых сборок под Windows —
# pip пытается компилировать их из исходников и падает без Visual Studio.
# httpx и aiosqlite — чистый Python: ставятся всегда и без компилятора.
#
# Реализовано ровно то, что нужно боту:
#   getMe, setMyCommands, getUpdates (long polling), sendMessage,
#   editMessageText, sendPhoto (по ссылке), answerCallbackQuery.
from __future__ import annotations

import httpx


class TgError(Exception):
    """Ошибка Telegram API (error_code + description)."""

    def __init__(self, code: int, description: str,
                 retry_after: int | None = None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class TgForbidden(TgError):
    """403 — бот заблокирован пользователем или чат недоступен."""


class TgBot:
    def __init__(self, token: str, api: str = "https://api.telegram.org",
                 proxy: str | None = None, verify: bool = True):
        self.token = token
        # таймаут больше long polling-а (25 сек) с запасом
        self._http = httpx.AsyncClient(
            base_url=api, timeout=httpx.Timeout(90.0, connect=15.0),
            proxy=proxy, verify=verify)

    # ── низкоуровневый вызов метода ────────────────────────────────
    async def call(self, method: str, payload: dict | None = None) -> dict:
        payload = {k: v for k, v in (payload or {}).items() if v is not None}
        r = await self._http.post(f"/bot{self.token}/{method}", json=payload)
        try:
            data = r.json()
        except ValueError:
            raise TgError(r.status_code, f"не-JSON ответ: {r.text[:120]}")
        if not data.get("ok"):
            code = data.get("error_code", 0)
            desc = data.get("description", "unknown error")
            retry = (data.get("parameters") or {}).get("retry_after")
            if code == 403:
                raise TgForbidden(code, desc)
            raise TgError(code, desc, retry)
        return data.get("result") or {}

    # ── удобные обёртки ────────────────────────────────────────────
    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def set_my_commands(self, commands: list[tuple[str, str]]):
        return await self.call("setMyCommands", {
            "commands": [{"command": c, "description": d} for c, d in commands]})

    async def get_updates(self, offset: int = 0, timeout: int = 25) -> list:
        return await self.call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": ["message", "callback_query"]})

    async def send_message(self, chat_id: int, text: str,
                           reply_markup: dict | None = None) -> dict:
        return await self.call("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": reply_markup})

    async def edit_message_text(self, chat_id: int, message_id: int, text: str,
                                reply_markup: dict | None = None) -> dict:
        return await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
            "reply_markup": reply_markup})

    async def send_photo(self, chat_id: int, photo: str,
                         caption: str | None = None,
                         reply_markup: dict | None = None) -> dict:
        return await self.call("sendPhoto", {
            "chat_id": chat_id, "photo": photo, "caption": caption,
            "parse_mode": "HTML", "reply_markup": reply_markup})

    async def answer_callback_query(self, callback_query_id: str,
                                    text: str | None = None) -> dict:
        return await self.call("answerCallbackQuery", {
            "callback_query_id": callback_query_id, "text": text})

    async def close(self):
        await self._http.aclose()
