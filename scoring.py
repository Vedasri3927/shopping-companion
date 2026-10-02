"""Buy Score + Deal Truth Check for shopping-companion.

    from scoring import score_results
    products = score_results(query, products)

Adds to each product: buy_score (0-100 or None), deal_verdict, deal_reason,
warranty (text or None), score_breakdown (per-factor 0-100 or None).
Factors with no data are left out and the rest re-weighted. Nothing is invented.
"""
import math
import re
from collections import Counter
import sqlite3
import statistics
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("price_history.db")
MIN_HISTORY = 5                    # past price points needed for the history factor
HISTORY_GAP_SECONDS = 12 * 3600    # prices saved in the last 12h don't count as "history"
MIN_REVIEWS = 5                    # ratings with fewer reviews are ignored
MIN_LISTINGS = 3                   # listings needed for spread / median comparisons

WEIGHTS = {
    "history": 0.20,   # price vs its own past prices
    "spread": 0.20,    # price vs other sellers now
    "rating": 0.20,    # star rating
    "reviews": 0.15,   # how many people rated it (confidence)
    "warranty": 0.15,  # warranty mentioned in the listing
    "seller": 0.10,    # known retailer / official brand store
}
WIDE_SPREAD = 6.0  # if p90/p10 of compared prices exceeds this, listings are mixed sizes/types
UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kgs?|kilograms?|grams?|gms?|g|ml|ltrs?|litres?|liters?|l)\b", re.I)
COUNT_RE = re.compile(r"(\d+)\s*(pcs|pc|pieces?|units?|count|tablets?|capsules?|sheets?|sticks?|bags?)\b", re.I)
PACK_RE = re.compile(r"(?:pack|set|combo)\s*of\s*(\d+)", re.I)
FLAGGED = ("Inflated discount", "Suspiciously low")

PRICE_KEYS = ("extracted_price", "price")
OLD_PRICE_KEYS = ("extracted_old_price", "old_price")
USED_RE = re.compile(r"refurb|renewed|pre-?owned|second[- ]?hand|open[- ]?box|\bused\b", re.I)
KNOWN_SELLERS = (
    "amazon", "flipkart", "croma", "reliance", "tata cliq", "tatacliq", "myntra", "ajio",
    "nykaa", "vijay sales", "jiomart", "apple", "samsung", "oneplus", "boat", "tanishq",
)


# ---------- helpers ----------
def to_number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = re.search(r"\d[\d,]*\.?\d*", str(value))
    if not m:
        return None
    try:
        n = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return n if n > 0 else None


def first_number(item, keys):
    for k in keys:
        n = to_number(item.get(k))
        if n is not None:
            return n
    return None


def linear(x, good, bad):
    """100 at `good`, 0 at `bad` (either direction)."""
    if good == bad:
        return 50.0
    return max(0.0, min(100.0, (x - bad) / (good - bad) * 100.0))


def money(n):
    return f"₹{n:,.0f}"


def query_key(query):
    return " ".join(re.findall(r"[a-z0-9]+", (query or "").lower()))


def is_used(item):
    return bool(USED_RE.search(str(item.get("title") or "")))


def listing_text(item):
    parts = [item.get("title"), item.get("snippet"), item.get("tag"), item.get("delivery")]
    ext = item.get("extensions")
    if isinstance(ext, (list, tuple)):
        parts.extend(ext)
    elif ext:
        parts.append(ext)
    return " ".join(str(p) for p in parts if p)


def parse_quantity(text):
    """Returns (amount, unit) with unit in {'g','ml','pc'} or None. Only reads what the listing says."""
    text = str(text or "")
    mult = 1
    pm = PACK_RE.search(text)
    if pm and 1 < int(pm.group(1)) <= 100:
        mult = int(pm.group(1))
    um = UNIT_RE.search(text)
    if um:
        n, u = float(um.group(1)), um.group(2).lower()
        if n > 0:
            if u.startswith("k"):
                return n * 1000 * mult, "g"
            if u.startswith("g"):
                return n * mult, "g"
            if u == "ml":
                return n * mult, "ml"
            return n * 1000 * mult, "ml"  # litres
    cm = COUNT_RE.search(text)
    if cm and int(cm.group(1)) > 0:
        return int(cm.group(1)) * mult, "pc"
    return None


def title_key(item):
    return (str(item.get("seller") or "").lower(), " ".join(re.findall(r"[a-z0-9]+", str(item.get("title") or "").lower())))


# ---------- price history (SQLite) ----------
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS prices (qkey TEXT, price REAL, ts INTEGER)")
    return con


def get_history(query):
    try:
        cutoff = int(time.time()) - HISTORY_GAP_SECONDS
        with _conn() as con:
            rows = con.execute(
                "SELECT price FROM prices WHERE qkey = ? AND ts < ?",
                (query, cutoff),
            ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def record_prices(query, prices):
    try:
        now = int(time.time())
        with _conn() as con:
            con.executemany(
                "INSERT INTO prices (qkey, price, ts) VALUES (?, ?, ?)",
                [(query, p, now) for p in prices],
            )
    except sqlite3.Error:
        pass


# ---------- factors ----------
def history_factor(price, history):
    if len(history) < MIN_HISTORY:
        return None
    return linear(price / statistics.median(history), 0.85, 1.30)


def susp_ratio(trusted):
    """Below this fraction of the typical price a listing looks suspicious. Trusted sellers get more slack."""
    return 0.3 if trusted else 0.5


def spread_factor(price, all_prices, trusted=False):
    if len(all_prices) < MIN_LISTINGS:
        return None
    median = statistics.median(all_prices)
    cheaper = sum(1 for p in all_prices if p < price)
    score = 100.0 * (1 - cheaper / (len(all_prices) - 1 or 1))
    if price < susp_ratio(trusted) * median:
        score = min(score, 50.0)  # suspiciously low: don't reward fully
    return score


def rating_factor(item):
    rating = to_number(item.get("rating"))
    reviews = to_number(item.get("reviews")) or 0
    if rating is None or rating > 5 or reviews < MIN_REVIEWS:
        return None
    return linear(rating, 4.7, 3.0)


def reviews_factor(item):
    reviews = to_number(item.get("reviews"))
    if not reviews:
        return 0.0  # no reviews = no social proof; unrated listings must not outrank well-reviewed ones
    return linear(math.log10(reviews), 3.0, 0.7)  # ~5 reviews = 0, 1000+ = 100


def warranty_factor(item):
    """Returns (score, label). Only reads what the listing itself says."""
    text = listing_text(item).lower()
    if re.search(r"\bno warranty\b|\bwithout warranty\b", text):
        return 0.0, "No warranty"
    months = None
    m = re.search(r"(\d+)\s*(year|yr|month)s?\s*(?:\w+\s){0,2}?(?:warranty|guarantee)", text) or \
        re.search(r"(?:warranty|guarantee)\s*(?:of|:)?\s*(\d+)\s*(year|yr|month)", text)
    if m:
        n = int(m.group(1))
        months = n * 12 if m.group(2) in ("year", "yr") else n
    if months is not None:
        label = f"{months // 12} year warranty" if months % 12 == 0 and months >= 12 else f"{months} month warranty"
        if months >= 12:
            return 100.0, label
        return (70.0 if months >= 6 else 40.0), label
    if re.search(r"warrant|guarantee", text):
        return 60.0, "Warranty mentioned"
    return None, None


def seller_factor(item, query_words, query_tokens=()):
    seller = str(item.get("seller") or item.get("source") or "").lower()
    if not seller:
        return None
    if any(k in seller for k in KNOWN_SELLERS):
        return 100.0
    words = {w for w in re.findall(r"[a-z0-9]+", seller) if len(w) > 2}
    if words & query_words:
        return 100.0  # official brand store (seller name shares a word with the query)
    # "maxfashion.in" vs the query "max fashion": seller name equals a run of adjacent query words joined
    core = re.sub(r"\.(co\.in|com|in|net|org|store|shop)$", "", seller.strip())
    core = re.sub(r"[^a-z0-9]", "", core)
    toks = list(query_tokens)
    for i in range(len(toks)):
        for j in range(i + 1, min(i + 4, len(toks)) + 1):
            if core and "".join(toks[i:j]) == core:
                return 100.0
    return None


# ---------- deal truth ----------
def deal_check(price, old_price, all_prices, history, fmt=money, trusted=False):
    if len(history) >= MIN_HISTORY:
        ref, ref_label = statistics.median(history), "its price history"
    elif len(all_prices) >= MIN_LISTINGS:
        ref, ref_label = statistics.median(all_prices), "similar listings"
    else:
        return "Not enough data", "Too few listings or history to judge this price."

    if old_price and old_price > price:
        disc = (old_price - price) / old_price
        if old_price >= 1.4 * ref and disc >= 0.2:
            return (
                "Inflated discount",
                f"Claims {disc:.0%} off {fmt(old_price)}, but based on {ref_label} "
                f"this typically sells around {fmt(ref)}.",
            )
        if susp_ratio(trusted) * ref <= price <= 0.9 * ref:
            return (
                "Genuine deal",
                f"{disc:.0%} off {fmt(old_price)} and below the typical "
                f"{fmt(ref)} from {ref_label}.",
            )

    if price < susp_ratio(trusted) * ref:
        return "Suspiciously low", f"Far below the typical {fmt(ref)} ({ref_label}). Verify the seller and model."
    if price <= 0.9 * ref:
        return "Below typical price", f"Cheaper than the typical {fmt(ref)} from {ref_label}."
    if price <= 1.15 * ref:
        return "Fair price", f"In line with the typical {fmt(ref)} from {ref_label}."
    return "Above typical price", f"Higher than the typical {fmt(ref)} from {ref_label}."


# ---------- main entry ----------
def score_results(query, items, record=True):
    q_tokens = re.findall(r"[a-z0-9]+", (query or "").lower())
    q_words = set(q_tokens)
    empty = {k: None for k in WEIGHTS}

    eligible = [i for i in items if first_number(i, PRICE_KEYS) and not is_used(i)]

    # 1) Are listings sold in different pack sizes? Normalise to price per unit when we can read sizes.
    qty = {id(i): parse_quantity(listing_text(i)) for i in eligible}
    units = Counter(v[1] for v in qty.values() if v)
    mode_unit = None
    if units:
        u, c = units.most_common(1)[0]
        if c >= 3 and c >= 0.5 * len(eligible):
            mode_unit = u
    scale = 100 if mode_unit in ("g", "ml") else 1
    suffix = {"g": " / 100 g", "ml": " / 100 ml", "pc": " / piece"}.get(mode_unit, "")

    def compared(item, price):
        if mode_unit is None:
            return price
        q = qty.get(id(item))
        if not q or q[1] != mode_unit:
            return None
        return price / q[0] * scale

    fmt = (lambda n: f"₹{n:,.1f}{suffix}") if mode_unit else money

    # 2) Same seller + same title at different prices = probably different sizes whose size isn't in the title.
    unclear = set()
    if mode_unit is None:
        groups = {}
        for i in eligible:
            groups.setdefault(title_key(i), []).append(i)
        for grp in groups.values():
            ps = [first_number(i, PRICE_KEYS) for i in grp]
            if len(grp) >= 2 and max(ps) > 1.5 * min(ps):
                unclear.update(id(i) for i in grp)

    comparable = {}
    for i in eligible:
        if id(i) in unclear:
            continue
        v = compared(i, first_number(i, PRICE_KEYS))
        if v is not None:
            comparable[id(i)] = v
    all_prices = list(comparable.values())

    # 3) Still a huge range? Then the listings are different products/sizes, so price comparison is unfair.
    wide = False
    if len(all_prices) >= MIN_LISTINGS:
        sp = sorted(all_prices)
        n = len(sp)
        p10, p90 = sp[int(0.1 * (n - 1))], sp[int(round(0.9 * (n - 1)))]
        wide = p10 > 0 and p90 / p10 > WIDE_SPREAD

    hist_key = query_key(query) + (":" + mode_unit if mode_unit else "")
    history = get_history(hist_key)  # read BEFORE recording this search

    for item in items:
        price = first_number(item, PRICE_KEYS)
        base = dict(warranty=None, score_breakdown=dict(empty), unit_price_label=None)
        if price is None:
            item.update(base, buy_score=None, deal_verdict="Not enough data", deal_reason="No usable price on this listing.")
            continue
        if is_used(item):
            item.update(base, buy_score=None, deal_verdict="Refurbished / used",
                        deal_reason="Not scored: refurbished or used units can't be fairly compared with new ones.")
            continue
        if id(item) in unclear:
            item.update(base, buy_score=None, deal_verdict="Pack size unclear",
                        deal_reason="Same seller lists this title at several prices, likely different pack sizes. Check the size before comparing.")
            continue
        val = comparable.get(id(item))
        if val is None:
            item.update(base, buy_score=None, deal_verdict="Pack size unclear",
                        deal_reason="Other listings state a size or quantity but this one doesn't, so its price can't be compared fairly.")
            continue

        old = first_number(item, OLD_PRICE_KEYS)
        if mode_unit and old:
            old = old / qty[id(item)][0] * scale
        w_score, w_label = warranty_factor(item)
        trusted = seller_factor(item, q_words, q_tokens) == 100.0 or (to_number(item.get("reviews")) or 0) >= 50
        factors = {
            "history": None if wide else history_factor(val, history),
            "spread": None if wide else spread_factor(val, all_prices, trusted),
            "rating": rating_factor(item),
            "reviews": reviews_factor(item),
            "warranty": w_score,
            "seller": seller_factor(item, q_words, q_tokens),
        }
        available = {k: v for k, v in factors.items() if v is not None}
        buy_score = None
        if len(available) >= 2:
            total_w = sum(WEIGHTS[k] for k in available)
            buy_score = int(round(sum(WEIGHTS[k] * v for k, v in available.items()) / total_w))

        if wide:
            verdict, reason = "Not comparable", "These listings vary too much in size or type to judge the price fairly."
        else:
            verdict, reason = deal_check(val, old, all_prices, history, fmt, trusted)
        if buy_score is not None and verdict in FLAGGED:
            buy_score = min(buy_score, 40)
        item["buy_score"] = buy_score
        item["deal_verdict"] = verdict
        item["deal_reason"] = reason
        item["warranty"] = w_label
        item["unit_price_label"] = fmt(val) if mode_unit else None
        item["score_breakdown"] = {k: (None if v is None else int(round(v))) for k, v in factors.items()}

    if record and all_prices and not wide:
        record_prices(hist_key, all_prices)
    return items


if __name__ == "__main__":
    import tempfile
    DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
    demo = [
        {"title": "boAt Airdopes 141 with 1 year warranty", "price": 1299, "seller": "Amazon.in", "rating": 4.3, "reviews": 8000},
        {"title": "Airdopes Alpha", "price": 999, "old_price": 2999, "seller": "Random Store", "rating": 4.4, "reviews": 300},
        {"title": "Airdopes 131", "price": 1099, "seller": "boAt Lifestyle", "extensions": ["6 months warranty"], "rating": 3.9, "reviews": 5},
        {"title": "Earbuds X", "price": 1199, "seller": "Some Seller"},
        {"title": "Earbuds Y no warranty", "price": 1249, "seller": "Flipkart", "rating": 4.0, "reviews": 40},
    ]
    for it in score_results("boat airdopes 141", demo):
        print(it["title"][:24], it["buy_score"], it["deal_verdict"], it["warranty"], it["score_breakdown"])
