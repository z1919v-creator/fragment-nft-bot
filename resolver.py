# resolver.py — поиск @username продавца через TON API
#
# Fragment показывает продавца как TON-кошелёк (иногда с .ton-доменом).
# Чтобы получить настоящий @username, проверяем, владеет ли кошелёк
# NFT из коллекции «Telegram Usernames» (все @usernames — это NFT в TON).
#
# Результаты кешируются в БД (wallet_cache), отрицательные — на WALLET_RECHECK_DAYS.
import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx

import db
from config import (PROXY, SSL_VERIFY, TONAPI_BASE, TONAPI_MIN_INTERVAL,
                    USERNAMES_COLLECTION, WALLET_RECHECK_DAYS)


class Resolver:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=25.0,
                                       headers={"User-Agent": "fragment-nft-bot/1.0"},
                                       proxy=PROXY, verify=SSL_VERIFY)
        self._lock = asyncio.Lock()     # тонап без ключа ≈ 1 req/s
        self._last_call = 0.0

    async def close(self):
        await self._http.aclose()

    async def ping(self):
        """Лёгкий запрос для диагностики доступности tonapi.io."""
        return await self._http.get(f"{TONAPI_BASE}/rates",
                                    params={"tokens": "ton", "currencies": "usd"})

    async def resolve(self, wallet: str, domain: str | None = None
                      ) -> tuple[str | None, str | None]:
        """Вернуть (@username, .ton-домен) для кошелька (или (None, domain))."""
        cached = await db.get_wallet(wallet)
        if cached is not None:
            username, c_domain, checked_at = cached
            if username:
                return username, (c_domain or domain)
            # username нет в кеше — перепроверяем не чаще раза в N дней
            try:
                checked = datetime.fromisoformat(checked_at)
                fresh = checked > datetime.now(timezone.utc) - timedelta(days=WALLET_RECHECK_DAYS)
            except (ValueError, TypeError):
                fresh = False
            if fresh:
                return None, (c_domain or domain)

        username = await self._lookup(wallet)
        final_domain = domain or (cached[1] if cached else None)
        await db.set_wallet(wallet, username, final_domain)
        return username, final_domain

    async def _lookup(self, wallet: str) -> str | None:
        async with self._lock:
            wait = TONAPI_MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            try:
                r = await self._http.get(
                    f"{TONAPI_BASE}/accounts/{wallet}/nfts",
                    params={"limit": 1000})
                if r.status_code != 200:
                    return None      # временная ошибка — вернём None, позже перепроверим
                for nft in r.json().get("nft_items", []):
                    coll = (nft.get("collection") or {}).get("address")
                    if coll == USERNAMES_COLLECTION:
                        name = ((nft.get("metadata") or {}).get("name") or "").strip()
                        name = name.lstrip("@").strip()
                        if name:
                            return name
            except (httpx.HTTPError, ValueError):
                return None
        return None
