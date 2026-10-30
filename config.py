# config.py — настройки бота
import os
from pathlib import Path

# ── Токен Telegram-бота (получен у @BotFather) ─────────────────────
# можно переопределить переменной окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "9076024497:AAGxk2Vj2P3TJyZNhvpc3C4qzp0ocqqPOgE")

# ── Общие ─────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(_BASE_DIR / "fragment.db"))  # SQLite-база (создаётся автоматически)
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))  # период проверки новых лотов (сек)

# ── Хостинг (Render) ───────────────────────────────────────────────
# OWNER_ID — твой Telegram ID: бот напишет тебе сам, если хостинг его
# перезапустит (на бесплатном Render база при перезапуске сбрасывается).
# Узнать свой ID можно прямо у бота — команда /start показывает его внизу.
def _int_env(name: str) -> int:
    try:
        return int((os.getenv(name) or "").strip() or 0)
    except ValueError:
        return 0


OWNER_ID = _int_env("OWNER_ID")          # 0 = не задан
SELF_PING_SECONDS = int(os.getenv("SELF_PING_SECONDS", "600"))  # как часто будить себя на Render

PEEK_COUNT = 5                # сколько свежих лотов показывать по кнопке «Лоты сейчас»
PEEK_SCAN_LIMIT = 12          # сколько лотов просмотреть в поисках тех, что с @username

# ── Сеть (если fragment.com недоступен/подменяется) ────────────────
# Если бот пишет «certificate verify failed» / «Hostname mismatch» —
# трафик до fragment.com перехватывается (блокировка провайдера, DPI или
# антивирус). Что можно сделать:
#   1) включить VPN в системном режиме (TUN / «прокси для всех приложений»)
#      и просто перезапустить бота;
#   2) указать прокси:  PROXY = "http://127.0.0.1:7890"
#      (порт — из настроек твоего VPN-клиента; работает и SOCKS5);
#   3) проверить файл hosts (C:\Windows\System32\drivers\etc\hosts) —
#      там не должно быть строк про fragment.com;
#   4) крайний вариант — SSL_VERIFY = False (отключить проверку
#      сертификатов; помогает, если HTTPS перехватывает антивирус).
PROXY = os.getenv("PROXY") or None
SSL_VERIFY = os.getenv("SSL_VERIFY", "1").strip().lower() not in ("0", "false", "no")

# ── Уведомления ─────────────────────────────────────────────────────
PRICE_DROP_COOLDOWN = int(os.getenv("PRICE_DROP_COOLDOWN", "1800"))  # сек между «снижение цены» по одному лоту

# ── TON API (поиск @username продавца) ─────────────────────────────
# публичный tonapi.io без ключа: держим интервал ~1 запрос/сек
TONAPI_BASE = "https://tonapi.io/v2"
TONAPI_MIN_INTERVAL = 1.1     # пауза между запросами к TON API (сек)
WALLET_RECHECK_DAYS = 7       # через сколько дней перепроверять кошелёк без @username

# адрес NFT-коллекции «Telegram Usernames» (для поиска @username владельца кошелька)
USERNAMES_COLLECTION = "0:80d78a35f955a14b679faa887ff4cd5bfc0f43b4a4eea2a7e6927f3701b273c2"

# ── Текст оффера для кнопки «Написать продавцу» ────────────────────
# доступные подстановки: {item} — название лота, {price} — цена в TON
OFFER_TEXT = ("Здравствуйте! Видел ваш лот {item} за {price} TON на Fragment. "
              "Готов обсудить покупку.")
