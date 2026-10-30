# parser.py — парсер NFT-подарков Fragment (fragment.com/gifts)
#
# Fragment отдаёт каталог серверным HTML — никакой авторизации не нужно:
#   /gifts?sort=listed&filter=sale          — все коллекции, новые сверху
#   /gifts/<slug>?sort=listed&filter=sale   — конкретная коллекция
#   /gift/<slug>-<номер>                    — страница лота (продавец, атрибуты)
import re
from dataclasses import dataclass, field
from html import unescape

import httpx

from config import PROXY, SSL_VERIFY

BASE = "https://fragment.com"
PHOTO_BASE = "https://nft.fragment.com/gift"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Listing:
    """Лот из каталога (карточка в сетке)."""
    listing_id: str                 # 'plushpepe-69' (уникален)
    collection: str                 # 'plushpepe' или 'all'
    name: str                       # 'Plush Pepe'
    num: str                        # '69'
    price_ton: float                # 15000.0
    status: str                     # 'For sale'
    url: str                        # https://fragment.com/gift/plushpepe-69
    photo: str                      # https://nft.fragment.com/gift/plushpepe-69.medium.jpg
    attrs: list = field(default_factory=list)   # [(label, value, rarity), ...]

    @property
    def title(self) -> str:
        return f"{self.name} #{self.num}"


@dataclass
class ItemInfo:
    """Данные со страницы конкретного лота."""
    wallet: str | None = None       # TON-кошелёк продавца (EQ...)
    domain: str | None = None       # .ton-домен продавца, если есть
    attrs: list = field(default_factory=list)  # [(Model, 'Gummy Frog', '3%'), ...]


# ── Регулярки (проверены на живом HTML fragment.com) ────────────────
CARD_RE = re.compile(
    r'<a href="/gift/(?P<slug>[a-z0-9][a-z0-9-]*)(?:\?[^"]*)?" '
    r'class="tm-grid-item">(?P<body>.*?)</a>', re.S)
NAME_RE = re.compile(r'<span class="item-name">([^<]*)</span>')
NUM_RE = re.compile(r'<span class="item-num">([^<]*)</span>')
PRICE_RE = re.compile(
    r'<div class="tm-grid-item-value tm-value icon-before icon-ton">([^<]+)</div>')
STATUS_RE = re.compile(r'<div class="tm-grid-item-status [^"]*">([^<]+)</div>')

CAT_RE = re.compile(
    r'<a href="/gifts/(?P<slug>[a-z0-9-]+)" class="tm-main-filters-item[^"]*"'
    r'[^>]*data-keywords="(?P<name>[^"]+)"[^>]*>(?P<body>.*?)</a>', re.S)
CAT_COUNT_RE = re.compile(r'tm-main-filters-count">([^<]+)<')

WALLET_RE = re.compile(r'gram\.org/address/([A-Za-z0-9_\-]{40,60})')
DOMAIN_RE = re.compile(r'<span class="short">([^<]+)</span>')
DOMAIN_OK_RE = re.compile(r'^[A-Za-z0-9_\-]+\.ton$')
ATTR_RE = re.compile(
    r'<div class="table-cell">\s*(?P<label>[A-Za-z][A-Za-z -]{1,24}?)\s*</div>\s*</td>\s*<td>\s*'
    r'<div class="table-cell">\s*<div class="table-cell-value tm-value">\s*'
    r'<a[^>]*class="table-cell-value-link">(?P<val>[^<]+)</a>'
    r'\s*(?:<span class="tm-rarity">\s*(?P<rar>[^<]+?)\s*</span>)?', re.S)
ITEM_STATUS_RE = re.compile(r'tm-section-header-status [^"]*">([^<]+)<')


# ── HTTP-клиент (ленивый синглтон) ──────────────────────────────────
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=25.0,
                                    follow_redirects=True,
                                    proxy=PROXY, verify=SSL_VERIFY)
    return _client


async def close():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ── Публичные функции ───────────────────────────────────────────────
async def fetch_categories() -> list[tuple[str, str, str]]:
    """Список всех коллекций подарков: [(slug, 'Plush Pepes', '3,048'), ...]."""
    r = await client().get(f"{BASE}/gifts")
    r.raise_for_status()
    cats: list[tuple[str, str, str]] = []
    for m in CAT_RE.finditer(r.text):
        slug = m.group("slug")
        name = unescape(m.group("name")).strip()
        cm = CAT_COUNT_RE.search(m.group("body"))
        count = unescape(cm.group(1)).strip() if cm else ""
        cats.append((slug, name, count))
    # дедуп по slug (на всякий случай)
    seen, uniq = set(), []
    for c in cats:
        if c[0] not in seen:
            seen.add(c[0])
            uniq.append(c)
    return uniq


async def fetch_listings(collection: str | None = None) -> list[Listing]:
    """Активные лоты (filter=sale), отсортированные по дате выставления.

    collection=None или 'all' — все коллекции; иначе слаг, например 'plushpepe'.
    Возвращает до 60 лотов (первая страница каталога).
    """
    path = "/gifts" if collection in (None, "all") else f"/gifts/{collection}"
    r = await client().get(f"{BASE}{path}",
                           params={"sort": "listed", "filter": "sale"})
    r.raise_for_status()

    out: list[Listing] = []
    for m in CARD_RE.finditer(r.text):
        body = m.group("body")
        nm = NAME_RE.search(body)
        if not nm:
            continue
        num_m = NUM_RE.search(body)
        pr = PRICE_RE.search(body)
        st = STATUS_RE.search(body)
        num = unescape(num_m.group(1)).replace("#", "").strip() if num_m else ""
        try:
            price = float(unescape(pr.group(1)).replace(",", "")) if pr else 0.0
        except ValueError:
            price = 0.0
        slug = m.group("slug")
        out.append(Listing(
            listing_id=slug,
            collection=collection or "all",
            name=unescape(nm.group(1)).strip(),
            num=num,
            price_ton=price,
            status=unescape(st.group(1)).strip() if st else "",
            url=f"{BASE}/gift/{slug}",
            photo=f"{PHOTO_BASE}/{slug}.medium.jpg",
        ))
    return out


async def fetch_item(slug: str) -> ItemInfo:
    """Страница лота: кошелёк продавца, .ton-домен, атрибуты (Model и т.п.)."""
    r = await client().get(f"{BASE}/gift/{slug}")
    r.raise_for_status()
    html = r.text

    info = ItemInfo()
    wm = WALLET_RE.search(html)
    if wm:
        info.wallet = wm.group(1)
        # .ton-домен ищем в той же ячейке таблицы (до конца строки <tr>)
        cell_end = html.find("</tr>", wm.end())
        cell = html[wm.end():cell_end if cell_end > 0 else wm.end() + 600]
        dm = DOMAIN_RE.search(cell)
        if dm and DOMAIN_OK_RE.match(dm.group(1).strip()):
            info.domain = dm.group(1).strip()

    info.attrs = [(m.group("label").strip(), unescape(m.group("val")).strip(),
                   unescape(m.group("rar") or "").strip())
                  for m in ATTR_RE.finditer(html)]
    return info
