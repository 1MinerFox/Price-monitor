import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:  # имитация TLS-отпечатка настоящего Chrome (помогает против блокировок)
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    http = requests
    IMPERSONATE = {}

# ====== НАСТРОЙКИ ======
CITY = "chelyabinsk"   # часть ссылки avito.ru/<город>
QUERIES = ["iphone 12", "iphone 13", "iphone 14", "iphone 15", "iphone 16", "iphone 17", "iphone air"]
MIN_PRICE = 10000      # руб., отсекает аксессуары и запчасти
MAX_PRICE = 200000
DISCOUNT = 0.15        # уведомлять, если дешевле медианы группы на 15% и больше
SCAM_LEVEL = 0.50      # если цена меньше 50% медианы - пометка "подозрительно"
MIN_SAMPLES = 6        # минимум объявлений в группе для расчёта медианы
MAX_HISTORY = 300      # сколько последних цен хранить на группу

BAD_WORDS = [
    "на запчасти", "запчаст", "разбит", "не включается", "не работает", "icloud",
    "айклауд", "заблокирован", "залочен", "блокировк", "чехол", "стекло", "защитн",
    "корпус", "куплю", "скупк", "обмен", "муляж", "макет", "битый", "трещин",
    "неисправ", "ремонт", "под восстановление", "аренда", "в аренду", "рассрочк",
    "кредит", "кабель", "зарядк",
]
# =======================

STATE_FILE = Path("state_avito.json")
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT"]

STORAGE_RE = re.compile(r"\b(64|128|256|512)\s?(?:gb|g)\b|\b([12])\s?tb\b")
MODEL_RE = re.compile(r"iphone\s*(1[2-7])\s*(pro\s*max|pro|plus|mini|air|e)?(?![a-z0-9])")
AIR_RE = re.compile(r"iphone\s*air")


def normalize(title: str) -> str:
    t = title.lower().replace("-", " ")
    for a, b in [
        (r"\bайфон\b", "iphone"), (r"\bпро\s*макс\b", "pro max"), (r"\bпромакс\b", "pro max"),
        (r"\bпро\b", "pro"), (r"\bплюс\b", "plus"), (r"\bмини\b", "mini"), (r"\bэйр\b", "air"),
        (r"\bгб\b", "gb"), (r"\bтб\b", "tb"), (r"\bgigabyte\b", "gb"),
    ]:
        t = re.sub(a, b, t)
    return t


def group_key(title: str):
    """Возвращает ключ вида '14 pro max 256GB' или None, если модель/память не определились."""
    t = normalize(title)
    if re.search(r"\b1[2-7]\s*(pro|max|plus|mini)?\s*/\s*(iphone\s*)?1[2-7]\b", t):
        return None               # заголовки вида "12/13" неоднозначны
    models = set()
    for m in MODEL_RE.finditer(t):
        variant = re.sub(r"\s+", " ", (m.group(2) or "").strip())
        models.add(f"{m.group(1)} {variant}".strip())
    if not models and AIR_RE.search(t):
        models.add("air")
    if len(models) != 1:
        return None
    st = {f"{a or b}{'GB' if a else 'TB'}" for a, b in STORAGE_RE.findall(t)}
    if len(st) != 1:
        return None
    return f"{models.pop()} {st.pop()}"


def send_telegram(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text},
        timeout=20,
    )


def parse_offers(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    for it in soup.select('[data-marker="item"]'):
        a = it.select_one('a[data-marker="item-title"]') or it.select_one('a[itemprop="url"]')
        pm = it.select_one('meta[itemprop="price"]')
        nm = it.select_one('[itemprop="name"]')
        if not a or not pm:
            continue
        href = a.get("href", "")
        if not href.startswith(f"/{CITY}"):
            continue  # пропускаем объявления из других городов
        try:
            price = float(pm.get("content", ""))
        except ValueError:
            continue
        title = (nm.get_text(" ", strip=True) if nm else "") or a.get("title", "") or a.get_text(" ", strip=True)
        iid = it.get("data-item-id") or re.search(r"_(\d+)$", href.split("?")[0] or "")
        iid = iid if isinstance(iid, str) else (iid.group(1) if iid else href)
        offers.append({"id": str(iid), "title": title, "url": "https://www.avito.ru" + href, "price": price})
    return offers


def fetch_offers(query: str) -> list[dict]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": "https://www.avito.ru/",
    }
    if not IMPERSONATE:
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    url = f"https://www.avito.ru/{CITY}/telefony/mobilnye_telefony"
    r = http.get(url, params={"q": query, "s": "104"}, headers=headers, timeout=30, **IMPERSONATE)
    print(f"[{query}] HTTP {r.status_code}, {len(r.text)} байт")
    if r.status_code != 200:
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))[:200]
        raise RuntimeError(f"HTTP {r.status_code}: {snippet}")
    offers = parse_offers(r.text)
    if not offers:
        title = re.search(r"<title>(.*?)</title>", r.text, re.S)
        print(f"[{query}] объявлений не найдено, заголовок страницы: {title.group(1).strip()[:150] if title else '?'}")
        if "firewall" in r.text.lower() or "доступ ограничен" in r.text.lower():
            raise RuntimeError("Авито показало страницу блокировки (IP/капча)")
    return offers


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"groups": {}, "seen": []}


def main() -> None:
    state = load_state()
    groups = state["groups"]
    seen = set(state["seen"])
    first_run = len(groups) == 0
    alerts = 0
    ok_queries = 0

    for q in QUERIES:
        try:
            offers = fetch_offers(q)
            ok_queries += 1
        except Exception as e:
            print(f"[{q}] ошибка: {e}")
            time.sleep(random.uniform(2, 4))
            continue
        time.sleep(random.uniform(2, 4))  # пауза, чтобы не выглядеть как бот

        for o in offers:
            if o["id"] in seen:
                continue
            title = o["title"].lower()
            if any(w in title for w in BAD_WORDS):
                continue
            if not (MIN_PRICE <= o["price"] <= MAX_PRICE):
                continue
            key = group_key(o["title"])
            if key is None:
                continue

            hist = groups.setdefault(key, {})
            prices = list(hist.values())
            if not first_run and len(prices) >= MIN_SAMPLES:
                median = statistics.median(prices)
                if o["price"] <= median * (1 - DISCOUNT):
                    drop = round((1 - o["price"] / median) * 100)
                    warn = "\n⚠️ Слишком дёшево - проверьте на мошенничество/блокировку" if o["price"] < median * SCAM_LEVEL else ""
                    send_telegram(
                        f"🔥 iPhone {key}: ниже рынка на {drop}%\n"
                        f"{o['title']}\n"
                        f"Цена: {o['price']:.0f} ₽ (медиана: {median:.0f}, выборка: {len(prices)})\n"
                        f"{o['url']}{warn}"
                    )
                    alerts += 1

            hist[o["id"]] = o["price"]
            seen.add(o["id"])

    for key, hist in groups.items():
        ids = list(hist.keys())[-MAX_HISTORY:]
        groups[key] = {i: hist[i] for i in ids}

    state["groups"] = groups
    state["seen"] = list(seen)[-5000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(f"Групп: {len(groups)}, уведомлений: {alerts}, успешных запросов: {ok_queries}/{len(QUERIES)}")
    if ok_queries == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
