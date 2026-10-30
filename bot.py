# bot.py — Telegram-бот: мониторинг NFT-подарков Fragment
# запуск:  python bot.py
#
# возможности:
#   • 📦 выбор категорий из 120+ коллекций Fragment + поиск (без «Отмены» —
#     поиск остаётся активным, запросы можно вводить один за другим)
#   • 🆕 авто-обновление по дате выставления: новые лоты приходят сами
#   • 👤 только продавцы с @username — тем, кому можно писать
#   • 💰 цена-максимум: уведомления только по лотам до N TON
#   • 📉 уведомления о снижении цены (с кулдауном на лот)
#   • 📊 статистика «сегодня выставлено N лотов» по каждой подписке
#
# работает на чистом httpx (модуль tg.py) — aiogram и его компилируемые
# зависимости (aiohttp, pydantic-core) не нужны, поэтому ставится на любой
# Python 3.10+ под Windows/macOS/Linux без Visual Studio.
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

import db
import httpx
import keep_alive
import parser as frag
from config import (BOT_TOKEN, OFFER_TEXT, OWNER_ID, PEEK_COUNT, PEEK_SCAN_LIMIT,
                    POLL_SECONDS, PRICE_DROP_COOLDOWN, PROXY, SELF_PING_SECONDS,
                    SSL_VERIFY)
from resolver import Resolver
from tg import TgBot, TgError, TgForbidden

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # меньше шума в консоли
log = logging.getLogger("bot")

resolver = Resolver()

PAGE_SIZE = 8              # категорий на страницу клавиатуры
CATS_TTL = 12 * 3600       # кеш списка категорий (сек)


# ────────────────────────────────────────────────────────────
#  Категории (кеш)
# ────────────────────────────────────────────────────────────
_cats_cache: list[tuple[str, str, str]] | None = None
_cats_ts = 0.0
_cats_fail_ts = 0.0             # не дёргать недоступный Fragment чаще раза в 30 с
_cats_err: str | None = None    # последняя ошибка — для понятного сообщения
_cats_lock = asyncio.Lock()     # чтобы пачка апдейтов не рванула за категориями разом


async def get_categories(force: bool = False):
    """Список коллекций (кеш 12 ч). None — если Fragment недоступен."""
    global _cats_cache, _cats_ts, _cats_fail_ts, _cats_err
    if not force and _cats_cache and time.monotonic() - _cats_ts < CATS_TTL:
        return _cats_cache
    if not force and _cats_cache is None and time.monotonic() - _cats_fail_ts < 30:
        return None                          # недавно падал — не молотим подряд
    async with _cats_lock:
        if not force and _cats_cache and time.monotonic() - _cats_ts < CATS_TTL:
            return _cats_cache                # сосед уже обновил кеш
        if not force and _cats_cache is None and time.monotonic() - _cats_fail_ts < 30:
            return None
        try:
            cats = await frag.fetch_categories()
        except Exception as e:
            _cats_err = str(e).splitlines()[0][:200] or repr(e)
            _cats_fail_ts = time.monotonic()
            log.warning("Fragment недоступен: %s", _cats_err)
            return _cats_cache
        if cats:
            _cats_cache = [("all", "Все коллекции", "")] + cats
            _cats_ts = time.monotonic()
            _cats_err = None
        return _cats_cache


def cat_name(slug: str) -> str:
    if _cats_cache:
        for s, name, _ in _cats_cache:
            if s == slug:
                return name
    return slug


# ────────────────────────────────────────────────────────────
#  Клавиатуры (обычные dict — Telegram Bot API)
# ────────────────────────────────────────────────────────────
def btn(text, data=None, url=None) -> dict:
    b = {"text": text}
    if data is not None:
        b["callback_data"] = data
    if url is not None:
        b["url"] = url
    return b


def kb(rows: list) -> dict:
    return {"inline_keyboard": rows}


def menu_kb() -> dict:
    return kb([
        [btn("📦 Категории", "cats"), btn("🔔 Подписки", "my")],
        [btn("📊 Статистика", "stats"), btn("⚙️ Настройки", "settings")],
        [btn("🆕 Проверить сейчас", "check")],
    ])


def cats_kb(page: int, my_subs: list[str]) -> dict | None:
    cats = _cats_cache or []
    total_pages = max(1, (len(cats) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = []
    for slug, name, count in cats[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        mark = "✅ " if slug in my_subs else ""
        extra = f" · {count}" if count else ""
        rows.append([btn(f"{mark}{name}{extra}", f"cat:{slug}")])
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"cats:{page - 1}"))
    nav.append(btn(f"{page + 1} / {total_pages}", "noop"))
    if page + 1 < total_pages:
        nav.append(btn("▶️", f"cats:{page + 1}"))
    rows.append(nav)
    rows.append([btn("🔍 Поиск", "search"), btn("↩️ Меню", "menu")])
    return kb(rows)


def my_kb(subs: list[str]) -> dict:
    rows = [[btn(f"✕ {cat_name(s)}", f"unsub:{s}")] for s in subs]
    rows.append([btn("🔇 Отписаться от всего", "unsuball"),
                 btn("↩️ Меню", "menu")])
    return kb(rows)


def after_sub_kb(slug: str) -> dict:
    return kb([
        [btn("🆕 Лоты сейчас", f"peek:{slug}"), btn("📊 Статистика", "stats")],
        [btn("↩️ Категории", "cats"), btn("↩️ Меню", "menu")],
    ])


def settings_kb() -> dict:
    return kb([
        [btn("💰 Цена-максимум", "setprice")],
        [btn("🩺 Диагностика сети", "diag")],
        [btn("↩️ Меню", "menu")],
    ])


def stats_kb() -> dict:
    return kb([
        [btn("🔄 Обновить", "stats"), btn("↩️ Меню", "menu")],
    ])


def listing_kb(username: str | None, url: str, wallet: str | None,
               price_ton: float, title: str) -> dict:
    rows = []
    if username:
        offer = quote(OFFER_TEXT.format(item=title, price=fmt_ton(price_ton)),
                      safe="")
        rows.append([btn("✍️ Написать продавцу",
                         url=f"https://t.me/{username}?text={offer}")])
    rows.append([btn("🔗 Лот на Fragment", url=url)])
    if wallet:
        rows.append([btn("👛 Кошелёк продавца", url=f"https://tonviewer.com/{wallet}")])
    return kb(rows)


# ────────────────────────────────────────────────────────────
#  Форматирование
# ────────────────────────────────────────────────────────────
def fmt_ton(p: float) -> str:
    if p == int(p):
        return f"{int(p):,}".replace(",", " ")
    return f"{p:,.2f}".replace(",", " ")


def attrs_lines(ls) -> list[str]:
    out = []
    for label, val, rar in (ls.attrs or [])[:3]:
        out.append(f"🎭 {escape(label)}: {escape(val)}" +
                   (f" · {escape(rar)}" if rar else ""))
    return out


def listing_caption(ls: frag.Listing, username: str) -> str:
    lines = [f"🆕 <b>{escape(ls.title)}</b>",
             f"💰 <b>{fmt_ton(ls.price_ton)} TON</b>"]
    lines += attrs_lines(ls)
    lines.append(f"👤 Продавец: @{escape(username)}")
    lines.append(f"🔗 {ls.url}")
    return "\n".join(lines)


def price_drop_caption(ls: frag.Listing, old_price: float, username: str) -> str:
    delta = old_price - ls.price_ton
    lines = ["📉 <b>Цена снизилась!</b>",
             f"🎁 <b>{escape(ls.title)}</b>",
             f"💰 <b>{fmt_ton(ls.price_ton)} TON</b> "
             f"<s>{fmt_ton(old_price)}</s> (−{fmt_ton(delta)})"]
    lines += attrs_lines(ls)
    lines.append(f"👤 Продавец: @{escape(username)}")
    lines.append(f"🔗 {ls.url}")
    return "\n".join(lines)


async def edit_msg(bot: TgBot, msg: dict, text: str, reply_markup: dict | None):
    """editMessageText с защитой от безобидных ошибок."""
    try:
        await bot.edit_message_text(msg["chat"]["id"], msg["message_id"],
                                    text, reply_markup)
    except TgError as e:
        d = e.description or ""
        if "message is not modified" in d:
            return
        if "message to edit not found" in d or "message can't be edited" in d:
            await bot.send_message(msg["chat"]["id"], text, reply_markup)
            return
        raise


async def safe_answer(bot: TgBot, cb_id: str, text: str | None = None):
    """answerCallbackQuery никогда не роняет обработчик.

    «query is too old» возникает, если юзер нажал кнопку в старом сообщении
    или пока бот был занят — это не ошибка, просто игнорируем."""
    try:
        await bot.answer_callback_query(cb_id, text)
    except TgError as e:
        log.debug("answerCallbackQuery: %s", e)


async def send_listing(bot: TgBot, chat_id: int, ls: frag.Listing,
                       wallet: str | None, domain: str | None,
                       username: str | None):
    kbd = listing_kb(username, ls.url, wallet, ls.price_ton, ls.title)
    cap = listing_caption(ls, username)
    try:
        await bot.send_photo(chat_id, ls.photo, cap, kbd)
    except TgForbidden:
        raise                              # юзер недоступен — фолбэк не нужен
    except TgError as e:                     # фото не отдалось — шлём текстом
        log.warning("send_photo %s: %s — отправляю текстом", ls.photo, e)
        await bot.send_message(chat_id, cap, kbd)


async def send_price_drop(bot: TgBot, chat_id: int, ls: frag.Listing,
                          old_price: float, wallet: str | None,
                          username: str):
    kbd = listing_kb(username, ls.url, wallet, ls.price_ton, ls.title)
    cap = price_drop_caption(ls, old_price, username)
    try:
        await bot.send_photo(chat_id, ls.photo, cap, kbd)
    except TgForbidden:
        raise
    except TgError as e:
        log.warning("send_photo %s: %s — отправляю текстом", ls.photo, e)
        await bot.send_message(chat_id, cap, kbd)


async def safe_send(coro_fn) -> bool:
    """Отправка с защитой от банов/флуда; False — юзер недоступен."""
    for _ in range(2):
        try:
            await coro_fn()
            return True
        except TgError as e:
            if isinstance(e, TgForbidden):
                return False
            if e.retry_after:
                await asyncio.sleep(e.retry_after + 1)
                continue
            log.error("send: %s", e)
            return False
        except Exception as e:
            log.error("send: %s", e)
            return False
    return True


async def user_price_ok(user_id: int, price: float) -> bool:
    pm = await db.get_price_max(user_id)
    return pm is None or price <= pm


# ────────────────────────────────────────────────────────────
#  Парсер-поллинг: одна коллекция
# ────────────────────────────────────────────────────────────
_poll_locks: dict[str, asyncio.Lock] = {}


def _coll_lock(collection: str) -> asyncio.Lock:
    return _poll_locks.setdefault(collection, asyncio.Lock())


async def poll_collection(bot: TgBot, collection: str) -> tuple[int, bool]:
    """Проверить коллекцию: новые лоты + снижения цен.

    Первый прогон — «тихий»: текущие лоты запоминаются (is_backfill=1)
    и не считаются «сегодняшними». Дальше:
      • новый лот → уведомление, если у продавца есть @username
        (кому можно писать) и цена в пределах лимита юзера
      • цена снизилась → уведомление 📉 (не чаще кулдауна на лот)
    """
    async with _coll_lock(collection):
        return await _poll_collection(bot, collection)


async def _poll_collection(bot: TgBot, collection: str) -> tuple[int, bool]:
    silent = await db.count_listings(collection) == 0
    listings = await frag.fetch_listings(collection)
    if not listings:
        log.warning("каталог %s пуст (сайт поменял разметку?)", collection)
        return 0, silent

    new, drops = [], []
    for ls in listings:
        row = await db.get_listing(ls.listing_id)
        if row is None:
            # ── новый лот ──
            wallet = domain = username = None
            if not silent:
                try:
                    info = await frag.fetch_item(ls.listing_id)
                    wallet, domain, ls.attrs = info.wallet, info.domain, info.attrs
                    if wallet:
                        username, domain = await resolver.resolve(wallet, domain)
                except Exception as e:
                    log.error("fetch_item %s: %s", ls.listing_id, e)
            await db.insert_listing(ls.listing_id, ls.collection, ls.name, ls.num,
                                    ls.price_ton, ls.url, wallet, domain, username,
                                    is_backfill=silent)
            if not silent and username:
                new.append((ls, wallet, domain, username))
        else:
            # ── снижение цены? ──
            old_price = row["price_ton"] or 0
            if not (ls.price_ton and old_price and ls.price_ton < old_price):
                continue
            wallet, domain = row["wallet"], row["domain"]
            username = row["username"]
            try:
                info = await frag.fetch_item(ls.listing_id)
                ls.attrs = info.attrs
                if info.wallet:
                    wallet, domain = info.wallet, info.domain
                    username, domain = await resolver.resolve(wallet, domain)
                await db.update_listing_seller(ls.listing_id, wallet, domain, username)
            except Exception as e:
                log.error("drop item %s: %s", ls.listing_id, e)
            cooldown_ok = True
            if row["last_drop"]:
                try:
                    last = datetime.fromisoformat(row["last_drop"])
                    cooldown_ok = ((datetime.now(timezone.utc) - last).total_seconds()
                                   > PRICE_DROP_COOLDOWN)
                except ValueError:
                    pass
            notify = bool(username) and cooldown_ok
            await db.update_listing_price(ls.listing_id, ls.price_ton,
                                          set_drop_time=notify)
            if notify:
                drops.append((ls, old_price, wallet, domain, username))

    users: list[int] = []
    if new or drops:
        users = await db.sub_users(collection)
        log.info("[poll] %s: +%d новых, %d снижений, подписчиков: %d",
                 collection, len(new), len(drops), len(users))

    for ls, wallet, domain, username in new:
        for uid in users:
            if not await user_price_ok(uid, ls.price_ton):
                continue
            ok = await safe_send(lambda: send_listing(bot, uid, ls, wallet,
                                                      domain, username))
            if not ok:
                await db.remove_user(uid)
                log.info("юзер %s недоступен — отписан", uid)

    for ls, old_price, wallet, domain, username in drops:
        for uid in users:
            if not await user_price_ok(uid, ls.price_ton):
                continue
            ok = await safe_send(lambda: send_price_drop(bot, uid, ls, old_price,
                                                         wallet, username))
            if not ok:
                await db.remove_user(uid)
                log.info("юзер %s недоступен — отписан", uid)

    return len(new), silent


async def poller(bot: TgBot):
    while True:
        try:
            colls = await db.subscribed_collections()
            for coll in colls:
                try:
                    await poll_collection(bot, coll)
                except Exception as e:
                    log.error("[poll] %s: %s", coll, e)
                await asyncio.sleep(0.7)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("[poller] %s", e)
        await asyncio.sleep(POLL_SECONDS)


# ────────────────────────────────────────────────────────────
#  Состояния ввода (вместо FSM): user_id -> "query" | "price"
# ────────────────────────────────────────────────────────────
states: dict[int, str] = {}

START_TEXT = (
    "🦊 <b>Fragment NFT Watch</b>\n"
    "Слежу за лотами подарков NFT на Fragment и присылаю только тех продавцов, "
    "которым можно написать.\n\n"
    "Что внутри:\n"
    "• 📦 <b>120+ коллекций</b> — Plush Pepe, Snoop Cigar, Loot Bag…\n"
    "• 🆕 новые лоты — сразу, как их выставили\n"
    "• 👤 только продавцы с @username — есть кнопка «Написать»\n"
    "• 💰 цена-максимум — слать только до N TON\n"
    "• 📉 уведомления о снижении цены\n"
    "• 📊 статистика «сегодня выставлено»\n\n"
    "Жми «Категории» и выбирай 👇"
)

PRICE_PROMPT = (
    "💰 Введи цену-максимум в TON — присылаю только лоты не дороже.\n"
    "Например: <code>500</code> или <code>1500.5</code>. "
    "Введи <code>0</code>, чтобы снять лимит."
)


async def settings_view(user_id: int) -> tuple[str, dict]:
    pm = await db.get_price_max(user_id)
    price_line = "без лимита" if pm is None else f"<b>{fmt_ton(pm)} TON</b>"
    text = ("⚙️ <b>Настройки</b>\n\n"
            f"💰 Цена-максимум: {price_line}\n\n"
            "Уведомления (новые лоты и снижения цены) приходят только по лотам "
            "не дороже лимита. Введи 0, чтобы снять лимит.")
    return text, settings_kb()


async def stats_view(user_id: int) -> tuple[str, dict]:
    subs = await db.get_subs(user_id)
    if not subs:
        return ("📊 Статистика появится, когда подпишешься на коллекции.\n"
                "Выбрать: 📦 Категории"), stats_kb()
    lines, total_today = [], 0
    for s in subs:
        today = await db.stats_today(s)
        total_today += today
        total = await db.count_listings(s)
        last = await db.last_new_time(s)
        line = (f"• <b>{escape(cat_name(s))}</b> — сегодня: <b>{today}</b>, "
                f"всего в базе: {total}")
        if last:
            line += f", последний: {last} МСК"
        lines.append(line)
    text = ("📊 <b>Статистика</b>\n\n" + "\n".join(lines) +
            f"\n\n🆕 Всего сегодня по твоим подпискам: <b>{total_today}</b>\n"
            "<i>Считаю по МСК. Лоты, которые висели до подписки, в счётчик "
            "не попадают.</i>")
    return text, stats_kb()


# ── Fragment недоступен: понятная ошибка вместо пустой клавиатуры ──
def cats_error_kb() -> dict:
    return kb([[btn("🩺 Диагностика сети", "diag")],
               [btn("🔄 Повторить", "cats"), btn("↩️ Меню", "menu")]])


def cats_error_text() -> str:
    err = escape(_cats_err or "нет ответа от fragment.com")
    return ("⚠️ <b>Fragment не отвечает</b> — список коллекций недоступен.\n\n"
            f"Причина: <code>{err}</code>\n\n"
            "Обычно это значит, что fragment.com недоступен с этого компьютера "
            "напрямую — например, сеть подменяет его сертификат "
            "(«Hostname mismatch» = блокировка провайдера или антивирус).\n\n"
            "Что попробовать:\n"
            "• включить VPN в системном режиме (TUN / «прокси для всех») "
            "и перезапустить бота\n"
            "• или запустить с прокси: <code>set HTTPS_PROXY=http://127.0.0.1:порт</code> "
            "перед <code>python bot.py</code>\n"
            "• или вписать <code>PROXY = \"http://127.0.0.1:порт\"</code> в config.py\n"
            "• проверить файл hosts и антивирус — подробнее в README\n\n"
            "Жми 🩺 Диагностика сети — проверю всё сразу.")


async def run_diag(bot: TgBot, chat_id: int):
    """Проверка доступности всех сервисов — чтобы понять, что именно блокируется."""
    status = await bot.send_message(chat_id,
                                    "🩺 Проверяю доступность сервисов (до минуты)…")
    results = []

    async def probe(name: str, fn):
        t0 = time.monotonic()
        try:
            r = await fn()
            code = getattr(r, "status_code", "ок")
            results.append(f"✅ {name} — {time.monotonic() - t0:.1f} с (HTTP {code})")
        except Exception as e:
            s = str(e).splitlines()[0][:140] or repr(e)
            if "CERTIFICATE" in s.upper() or "SSL" in s.upper():
                s += " → сертификат подменён (блокировка или антивирус)"
            results.append(f"❌ {name} — {s}")

    await probe("fragment.com (каталог подарков)",
                lambda: frag.client().get(f"{frag.BASE}/gifts"))
    await probe("nft.fragment.com (фото лотов)",
                lambda: frag.client().get(f"{frag.PHOTO_BASE}/plushpepe-1.medium.jpg"))
    await probe("tonapi.io (@username продавцов)", resolver.ping)
    await probe("api.telegram.org (сам бот)", bot.get_me)

    text = ("🩺 <b>Диагностика сети</b>\n\n" + "\n".join(results) +
            "\n\nЕсли fragment.com ❌ — трафик до него блокируется или "
            "перехватывается: включи VPN системно (TUN), задай HTTPS_PROXY "
            "или PROXY в config.py и перезапусти бота.\n"
            "Если всё ✅, но лоты не приходят — нажми «🆕 Проверить сейчас».")
    try:
        await bot.edit_message_text(chat_id, status["message_id"], text)
    except TgError:
        await bot.send_message(chat_id, text)


# ────────────────────────────────────────────────────────────
#  Команды
# ────────────────────────────────────────────────────────────
async def cmd_start(bot: TgBot, m: dict):
    await db.add_user(m["from"]["id"])
    # прогрев кеша категорий — в фоне, чтобы меню появилось мгновенно,
    # даже если fragment.com недоступен
    t = asyncio.create_task(get_categories())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    text = START_TEXT + f"\n🆔 Твой ID: <code>{m['from']['id']}</code>"
    await bot.send_message(m["chat"]["id"], text, menu_kb())


async def cmd_cats(bot: TgBot, m: dict):
    cats = await get_categories()
    if cats is None:
        await bot.send_message(m["chat"]["id"], cats_error_text(), cats_error_kb())
        return
    await bot.send_message(
        m["chat"]["id"], "📦 Выбери коллекцию — подпишешься на её новые лоты:",
        cats_kb(0, await db.get_subs(m["from"]["id"])))


async def cmd_my(bot: TgBot, m: dict):
    uid = m["from"]["id"]
    subs = await db.get_subs(uid)
    if not subs:
        await bot.send_message(m["chat"]["id"],
                               "🔔 Подписок пока нет — выбери категории: /cats")
        return
    names = "\n".join(f"• {cat_name(s)}" for s in subs)
    await bot.send_message(m["chat"]["id"],
                           f"🔔 Твои подписки ({len(subs)}):\n{escape(names)}",
                           my_kb(subs))


async def cmd_now(bot: TgBot, m: dict):
    await run_check(bot, m["from"]["id"], m["chat"]["id"])


async def cmd_stats(bot: TgBot, m: dict):
    text, kbd = await stats_view(m["from"]["id"])
    await bot.send_message(m["chat"]["id"], text, kbd)


async def cmd_price(bot: TgBot, m: dict):
    states[m["from"]["id"]] = "price"
    await bot.send_message(m["chat"]["id"], PRICE_PROMPT,
                           kb([[btn("↩️ Отмена", "settings")]]))


async def cmd_stopall(bot: TgBot, m: dict):
    n = await db.unsub_all(m["from"]["id"])
    await bot.send_message(
        m["chat"]["id"],
        f"🔇 Отписан от {n} коллекций." if n else "Подписок и так нет 🙂")


async def cmd_diag(bot: TgBot, m: dict):
    await run_diag(bot, m["chat"]["id"])


COMMANDS = {
    "start": cmd_start,
    "cats": cmd_cats,
    "my": cmd_my,
    "now": cmd_now,
    "stats": cmd_stats,
    "price": cmd_price,
    "diag": cmd_diag,
    "stopall": cmd_stopall,
}


async def handle_message(bot: TgBot, m: dict):
    text = (m.get("text") or "").strip()
    if not text:
        return
    uid, cid = m["from"]["id"], m["chat"]["id"]

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower().lstrip("/")
        if cmd in COMMANDS:
            states.pop(uid, None)
            await COMMANDS[cmd](bot, m)
            return
        if states.get(uid) == "query":       # неизвестная команда → выход из поиска
            states.pop(uid, None)
            await bot.send_message(cid, START_TEXT +
                                   f"\n🆔 Твой ID: <code>{uid}</code>", menu_kb())
            return
        if states.get(uid) == "price":
            await on_price_input(bot, m)
            return
        await bot.send_message(cid, "🤔 Не понял команду — пользуйся меню 👇",
                               menu_kb())
        return

    st = states.get(uid)
    if st == "query":
        await on_search_query(bot, m)
        return
    if st == "price":
        await on_price_input(bot, m)
        return
    await bot.send_message(cid, "🤔 Не понял команду — пользуйся меню 👇",
                           menu_kb())


# ────────────────────────────────────────────────────────────
#  Колбэки кнопок
# ────────────────────────────────────────────────────────────
async def handle_callback(bot: TgBot, cb: dict):
    data = cb.get("data") or ""
    uid = cb["from"]["id"]
    msg = cb.get("message")
    cb_id = cb["id"]

    if not msg:                                # старое сообщение — просто ответим
        await safe_answer(bot, cb_id)
        return

    # ── меню ──
    if data == "menu":
        states.pop(uid, None)
        await edit_msg(bot, msg, START_TEXT, menu_kb())
        await safe_answer(bot, cb_id)

    elif data == "noop":
        await safe_answer(bot, cb_id)

    elif data == "cats":
        states.pop(uid, None)
        await safe_answer(bot, cb_id)          # отвечаем сразу — кнопка не «висит»
        cats = await get_categories()
        if cats is None:
            await edit_msg(bot, msg, cats_error_text(), cats_error_kb())
            return
        await edit_msg(bot, msg,
                       "📦 Выбери коллекцию — подпишешься на её новые лоты:",
                       cats_kb(0, await db.get_subs(uid)))

    elif data.startswith("cats:"):
        states.pop(uid, None)
        await safe_answer(bot, cb_id)          # отвечаем сразу
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        cats = _cats_cache
        if not cats:
            await edit_msg(bot, msg, cats_error_text(), cats_error_kb())
            return
        await edit_msg(bot, msg,
                       "📦 Выбери коллекцию — подпишешься на её новые лоты:",
                       cats_kb(page, await db.get_subs(uid)))

    elif data == "my":
        states.pop(uid, None)
        subs = await db.get_subs(uid)
        if not subs:
            await edit_msg(bot, msg, "🔔 Подписок пока нет.",
                           kb([[btn("📦 К категориям", "cats")],
                               [btn("↩️ Меню", "menu")]]))
            await safe_answer(bot, cb_id)
            return
        names = "\n".join(f"• {cat_name(s)}" for s in subs)
        await edit_msg(bot, msg,
                       f"🔔 Твои подписки ({len(subs)}):\n{escape(names)}",
                       my_kb(subs))
        await safe_answer(bot, cb_id)

    elif data == "unsuball":
        states.pop(uid, None)
        n = await db.unsub_all(uid)
        await edit_msg(bot, msg,
                       f"🔇 Отписан от {n} коллекций." if n
                       else "Подписок и так нет 🙂",
                       menu_kb())
        await safe_answer(bot, cb_id)

    elif data.startswith("unsub:"):
        states.pop(uid, None)
        slug = data.split(":", 1)[1]
        await db.set_sub(uid, slug, False)
        subs = await db.get_subs(uid)
        if not subs:
            await edit_msg(bot, msg, "🔕 Готово, подписок больше нет.",
                           kb([[btn("📦 К категориям", "cats")],
                               [btn("↩️ Меню", "menu")]]))
            await safe_answer(bot, cb_id, "Отписка")
            return
        names = "\n".join(f"• {cat_name(s)}" for s in subs)
        await edit_msg(bot, msg,
                       f"🔔 Твои подписки ({len(subs)}):\n{escape(names)}",
                       my_kb(subs))
        await safe_answer(bot, cb_id, "Отписка")

    # ── поиск: остаётся активным, отменять не нужно ──
    elif data == "search":
        states[uid] = "query"
        await edit_msg(
            bot, msg,
            "🔍 <b>Поиск коллекций</b>\n\n"
            "Просто пиши слова — например <code>pepe</code>, <code>loot</code>, "
            "<code>snoop</code>.\n\n"
            "Поиск остаётся активным: вводи запросы один за другим, "
            "ничего нажимать не нужно.\n\n"
            "Выход — кнопка ниже или /start.",
            kb([[btn("↩️ Выйти в меню", "menu")]]))
        await safe_answer(bot, cb_id)

    # ── подписка/отписка ──
    elif data.startswith("cat:"):
        states.pop(uid, None)
        slug = data.split(":", 1)[1]
        subs = await db.get_subs(uid)
        if slug in subs:
            await db.set_sub(uid, slug, False)
            await edit_msg(
                bot, msg,
                f"🔕 Подписка снята: <b>{escape(cat_name(slug))}</b>\n\n"
                "Новые лоты по ней приходить не будут.",
                kb([[btn("↩️ К категориям", "cats"),
                     btn("↩️ Меню", "menu")]]))
            await safe_answer(bot, cb_id, "Отписка")
            return
        await db.set_sub(uid, slug, True)
        await edit_msg(
            bot, msg,
            f"🔔 Подписка оформлена: <b>{escape(cat_name(slug))}</b>\n\n"
            "• 🆕 пришлю новые лоты сразу, как их выставят\n"
            "• 👤 только продавцов с @username — им можно написать\n"
            "• 💰 в пределах твоего лимита цены (если задан)\n"
            "• 📉 сообщу, если продавец снизит цену\n\n"
            "Первый опрос тихий: запомню текущие лоты, спама историей не будет.\n"
            "Посмотреть, что уже продаётся?",
            after_sub_kb(slug))
        await safe_answer(bot, cb_id, "Подписка оформлена")

    # ── «Лоты сейчас»: только продавцы с @username + лимит цены ──
    elif data.startswith("peek:"):
        states.pop(uid, None)
        slug = data.split(":", 1)[1]
        cid = msg["chat"]["id"]
        await safe_answer(bot, cb_id, "⏳ Смотрю…")
        pm = await db.get_price_max(uid)
        status = await bot.send_message(
            cid, f"⏳ Ищу свежие лоты «{escape(cat_name(slug))}» "
                 "с продавцами, которым можно написать…")
        try:
            listings = await frag.fetch_listings(slug)
        except Exception as e:
            log.error("peek %s: %s", slug, e)
            await bot.edit_message_text(
                cid, status["message_id"],
                "⚠️ Fragment не отвечает — возможно, нужен VPN/прокси.\n"
                "Проверить: ⚙️ Настройки → 🩺 Диагностика сети.")
            return
        if not listings:
            await bot.edit_message_text(cid, status["message_id"],
                                        "Пусто — активных лотов не нашлось.")
            return
        shown = no_user = over_price = 0
        for ls in listings[:PEEK_SCAN_LIMIT]:
            if shown >= PEEK_COUNT:
                break
            try:
                info = await frag.fetch_item(ls.listing_id)
            except Exception as e:
                log.error("peek item %s: %s", ls.listing_id, e)
                continue
            wallet, domain, ls.attrs = info.wallet, info.domain, info.attrs
            username = None
            if wallet:
                username, domain = await resolver.resolve(wallet, domain)
            if not username:
                no_user += 1
                continue
            if pm is not None and ls.price_ton > pm:
                over_price += 1
                continue
            await send_listing(bot, uid, ls, wallet, domain, username)
            shown += 1
        parts = [f"✅ Показал {shown} лотов с @username"]
        if no_user:
            parts.append(f"пропустил {no_user} без юзернейма")
        if over_price:
            parts.append(f"{over_price} дороже лимита")
        await bot.edit_message_text(cid, status["message_id"],
                                    ". ".join(parts) +
                                    ". Новые будут приходить сами 🚀")

    # ── настройки: цена-максимум ──
    elif data == "settings":
        states.pop(uid, None)
        text, kbd = await settings_view(uid)
        await edit_msg(bot, msg, text, kbd)
        await safe_answer(bot, cb_id)

    elif data == "setprice":
        states[uid] = "price"
        await edit_msg(bot, msg, PRICE_PROMPT,
                       kb([[btn("↩️ Отмена", "settings")]]))
        await safe_answer(bot, cb_id)

    # ── статистика ──
    elif data == "stats":
        states.pop(uid, None)
        text, kbd = await stats_view(uid)
        await edit_msg(bot, msg, text, kbd)
        await safe_answer(bot, cb_id)

    # ── диагностика сети ──
    elif data == "diag":
        await safe_answer(bot, cb_id)
        await run_diag(bot, uid, msg["chat"]["id"])

    # ── «Проверить сейчас» ──
    elif data == "check":
        states.pop(uid, None)
        await safe_answer(bot, cb_id)
        await run_check(bot, uid, msg["chat"]["id"])

    else:
        await safe_answer(bot, cb_id)


# ────────────────────────────────────────────────────────────
#  Ввод текста в состояниях
# ────────────────────────────────────────────────────────────
async def on_search_query(bot: TgBot, m: dict):
    uid, cid = m["from"]["id"], m["chat"]["id"]
    q = m["text"].strip().lower()
    cats = await get_categories()
    if not cats:
        await bot.send_message(cid, "⚠️ Не удалось получить список категорий, "
                                    "попробуй позже.")
        return
    matched = [c for c in cats if q in c[0].lower() or q in c[1].lower()]
    if not matched:
        await bot.send_message(cid, f"🤷 По «{escape(q)}» ничего нет. Введи другой "
                                    "запрос — поиск ещё активен 🔍")
        return
    my_subs = await db.get_subs(uid)
    rows = []
    for slug, name, count in matched[:10]:
        mark = "✅ " if slug in my_subs else ""
        extra = f" · {count}" if count else ""
        rows.append([btn(f"{mark}{name}{extra}", f"cat:{slug}")])
    rows.append([btn("↩️ Меню", "menu")])
    await bot.send_message(cid, f"🔍 Нашёл {len(matched)}. Жми, чтобы подписаться —\n"
                                "следующий запрос можно вводить сразу 🔍",
                           kb(rows))


async def on_price_input(bot: TgBot, m: dict):
    uid, cid = m["from"]["id"], m["chat"]["id"]
    raw = (m["text"].strip().replace(" ", "").replace("\u00a0", "")
           .replace(",", "."))
    try:
        val = float(raw)
    except ValueError:
        await bot.send_message(cid, "🤔 Не похоже на число. Введи цену в TON, "
                                    "например <code>500</code>, или <code>0</code> — "
                                    "снять лимит.")
        return
    if val < 0:
        await bot.send_message(cid, "🤔 Цена не может быть отрицательной. "
                                    "Попробуй ещё раз.")
        return
    await db.set_price_max(uid, None if val == 0 else val)
    states.pop(uid, None)
    text, kbd = await settings_view(uid)
    prefix = ("✅ Лимит снят — слать лоты любых цен.\n\n" if val == 0 else
              f"✅ Сохранено: слать только до {fmt_ton(val)} TON.\n\n")
    await bot.send_message(cid, prefix + text, kbd)


# ────────────────────────────────────────────────────────────
#  «Проверить сейчас»
# ────────────────────────────────────────────────────────────
async def run_check(bot: TgBot, user_id: int, chat_id: int):
    subs = await db.get_subs(user_id)
    if not subs:
        await bot.send_message(chat_id, "Сначала подпишись на категории: /cats")
        return
    status = await bot.send_message(chat_id,
                                    "⏳ Проверяю твои подписки (до минуты)…")
    total, silent_any = 0, False
    for coll in subs:
        try:
            n, silent = await poll_collection(bot, coll)
            total += n
            silent_any = silent_any or silent
        except Exception as e:
            log.error("check %s: %s", coll, e)
    if total:
        text = f"✅ Готово: {total} новых лотов — отправил выше."
    elif silent_any:
        text = ("✅ Готово: новых лотов нет.\n"
                "(Первый опрос: запомнил текущие лоты, дальше "
                "буду слать только новые.)")
    else:
        text = "✅ Готово: новых лотов пока нет."
    await bot.edit_message_text(chat_id, status["message_id"], text)


# ────────────────────────────────────────────────────────────
#  Цикл обновлений
# ────────────────────────────────────────────────────────────
_tasks: set[asyncio.Task] = set()


async def handle_update(bot: TgBot, upd: dict):
    if "message" in upd:
        await handle_message(bot, upd["message"])
    elif "callback_query" in upd:
        await handle_callback(bot, upd["callback_query"])


async def _safe_handle(bot: TgBot, upd: dict):
    try:
        await handle_update(bot, upd)
    except TgForbidden:
        pass
    except Exception as e:
        log.error("обработка апдейта: %s", e, exc_info=True)


def _spawn(bot: TgBot, upd: dict):
    t = asyncio.create_task(_safe_handle(bot, upd))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def main():
    # чтобы кириллица и эмодзи не падали в консоли Windows
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # ── Render: занять PORT и начать «будить» себя, чтобы не усыпили ──
    hosting = bool(os.getenv("RENDER_EXTERNAL_URL"))
    port = int(os.getenv("PORT") or 0)
    if port:
        keep_alive.start(port)
    if hosting:
        asyncio.get_running_loop().create_task(
            _self_ping(os.getenv("RENDER_EXTERNAL_URL")))

    await db.init()
    bot = TgBot(BOT_TOKEN, proxy=PROXY, verify=SSL_VERIFY)
    await bot.set_my_commands([
        ("start", "меню бота"),
        ("cats", "категории подарков"),
        ("my", "мои подписки"),
        ("now", "проверить сейчас"),
        ("stats", "статистика за сегодня"),
        ("price", "цена-максимум, TON"),
        ("diag", "проверка доступности сервисов"),
        ("stopall", "отписаться от всего"),
    ])
    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.get("username"))

    if hosting:
        await _notify_owner(bot)

    poller_task = asyncio.create_task(poller(bot))
    offset = 0
    last_409_log = 0.0
    try:
        while True:
            try:
                updates = await bot.get_updates(offset)
            except asyncio.CancelledError:
                raise
            except TgError as e:
                if e.code == 409:
                    # бот уже запущен где-то ещё (например, остался на Windows
                    # или это старый деплой) — не падаем, ждём и пробуем снова
                    if time.monotonic() - last_409_log > 60:
                        log.warning(
                            "409: бот уже запущен в другом месте (Windows/другой "
                            "сервис?). Оставь работать только ОДИН экземпляр.")
                        last_409_log = time.monotonic()
                    await asyncio.sleep(10)
                    continue
                log.error("getUpdates: %s", e)
                await asyncio.sleep(3)
                continue
            except Exception as e:
                log.error("getUpdates: %s", e)
                await asyncio.sleep(3)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                _spawn(bot, upd)
    finally:
        poller_task.cancel()
        for t in list(_tasks):
            t.cancel()
        await resolver.close()
        await frag.close()
        await bot.close()
        log.info("Бот остановлен")


async def _self_ping(base_url: str) -> None:
    """Каждые SELF_PING_SECONDS дёргаем сами себя по внешнему адресу —
    иначе бесплатный Render усыпляет сервис через 15 минут тишины."""
    url = base_url.rstrip("/") + "/health"
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            try:
                r = await client.get(url)
                log.info("self-ping %s (не даём хостингу уснуть)", r.status_code)
            except Exception as e:
                log.warning("self-ping не прошёл: %s", e)
            await asyncio.sleep(SELF_PING_SECONDS)


async def _notify_owner(bot: TgBot) -> None:
    """Написать владельцу при старте на хостинге.

    Бесплатный Render хранит базу во временной файловой системе: после
    перезапуска подписки сбрасываются. Чтобы это не было сюрпризом —
    предупреждаем владельца прямо в чате."""
    if not OWNER_ID:
        return
    fresh = not await db.has_users()
    text = "🚀 Бот поднялся на хостинге и работает 24/7."
    if fresh:
        text += ("\n\n⚠️ База пустая — похоже, хостинг перезапустил бота, "
                 "и подписки сбросились. Нажми /start и выбери категории "
                 "заново (/cats). Это особенность бесплатного тарифа.")
    else:
        text += "\nПодписки на месте, всё в порядке ✅"
    try:
        await bot.send_message(OWNER_ID, text)
    except TgError as e:
        log.warning("не удалось написать владельцу %s: %s", OWNER_ID, e)


if __name__ == "__main__":
    asyncio.run(main())
