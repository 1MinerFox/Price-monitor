import json
import os
import re
import statistics
from pathlib import Path

import requests

# ====== НАСТРОЙКИ ======
QUERIES = ["iphone 12", "iphone 13", "iphone 14", "iphone 15", "iphone 16", "iphone 17", "iphone air"]
PAGES = 2             # сколько страниц (по 50 объявлений) брать на каждый запрос
MIN_PRICE = 500       # PLN, отсекает аксессуары и запчасти
MAX_PRICE = 8000
DISCOUNT = 0.15       # уведомлять, если дешевле медианы группы на 15% и больше
SCAM_LEVEL = 0.50     # если цена меньше 50% медианы - пометка "подозрительно"
MIN_SAMPLES = 8       # минимум объявлений в группе для расчёта медианы
MAX_HISTORY = 300     # сколько последних цен хранить на группу

BAD_WORDS = [
    "uszkodzony", "uszkodzona", "uszkodzone", "na części", "na czesci", "zablokowany",
    "icloud", "etui", "szkło", "szklo", "obudowa", "kupię", "kupie", "wymiana",
    "zamienię", "zamienie", "do naprawy", "nie działa", "nie dziala", "pokrowiec",
    "ładowarka", "ladowarka", "kabel", "atrapa", "makieta", "bez ekranu", "rozbity",
    "pęknięty", "peknięty", "cracked", "case", "lock", "blokada",
]
# =======================

API = "https://www.olx.pl/api/v1/offers/"
STATE_FILE = Path("state.json")
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT"]

MODEL_RE = re.compile(r"iphone\s*(1[2-7])\s*(pro\s*max|pro|plus|mini|air|e)?(?![a-z0-9])")
AIR_RE = re.compile(r"iphone\s*air")
STORAGE_RE = re.compile(r"\b(64|128|256|512)\s?gb\b|\b([12])\s?tb\b")


def group_key(title: str):
    """Возвращает ключ вида '14 pro max 256GB' или None, если модель/память не определились."""
    t = title.lower().replace("-", " ")
    if re.search(r"\b1[2-7]\s*(pro|max|plus|mini)?\s*/\s*(iphone\s*)?1[2-7]\b", t):
        return None               # заголовки вида "12/13" неоднозначны
    models = set()
    for m in MODEL_RE.finditer(t):
        variant = re.sub(r"\s+", " ", (m.group(2) or "").strip())
        models.add(f"{m.group(1)} {variant}".strip())
    if not models and AIR_RE.search(t):
        models.add("air")
    if len(models) != 1:          # не нашли или в заголовке несколько моделей - пропускаем
        return None
    st = {f"{a or b}{'GB' if a else 'TB'}" for a, b in STORAGE_RE.findall(t)}
    if len(st) != 1:              # память не указана или неоднозначна
        return None
    return f"{models.pop()} {st.pop()}"


def send_telegram(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text},
        timeout=20,
    )


def fetch_offers(query: str) -> list[dict]:
    offers = []
    for page in range(PAGES):
        r = requests.get(
            API,
            params={"query": query, "limit": 50, "offset": page * 50, "sort_by": "created_at:desc"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        r.raise_for_status()
        for o in r.json().get("data", []):
            price = None
            for p in o.get("params", []):
                if p.get("key") == "price":
                    price = p.get("value", {}).get("value")
            if price is None:
                continue
            offers.append(
                {"id": str(o["id"]), "title": o.get("title", ""), "url": o.get("url", ""), "price": float(price)}
            )
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

    for q in QUERIES:
        try:
            offers = fetch_offers(q)
        except Exception as e:
            print(f"[{q}] ошибка: {e}")
            continue

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
                        f"Цена: {o['price']:.0f} PLN (медиана: {median:.0f}, выборка: {len(prices)})\n"
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
    print(f"Групп: {len(groups)}, уведомлений: {alerts}")


if __name__ == "__main__":
    main()
