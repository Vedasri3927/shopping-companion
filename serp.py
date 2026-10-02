import hashlib
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

SERPAPI_URL = "https://serpapi.com/search.json"

# Common filler words that shouldn't count when checking if a result matches the query.
_STOPWORDS = {
    "for", "with", "the", "a", "an", "and", "or", "of", "in", "on",
    "men", "women", "kids", "new", "best", "buy",
    "size", "xs", "xl", "xxl", "small", "medium", "large",
    "under", "below", "above", "upto", "price", "online", "india", "cheap",
}
# Below this fraction of matching significant words, a result is considered a mismatch.
_RELEVANCE_THRESHOLD = 0.5
# If the strict filter leaves fewer than this many results, fill up with the next-best matches.
_MIN_RESULTS = 5


def _stem(w: str) -> str:
    """Crude plural handling so 'tops' matches 'top' and 'earrings' matches 'earring'."""
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w

# Titles containing these words are accessories/parts for a product, not the product
# itself (a "boat airdopes 141" search shouldn't be dominated by cases for it).
_ACCESSORY_WORDS_RAW = {
    "case", "cover", "skin", "strap", "sticker", "protector", "pouch",
    "holder", "charger", "cable", "adapter", "mount", "tempered",
    "screenguard", "screen", "band", "belt", "pendrive", "spare",
    "replacement", "repair", "kit", "cleaning", "cleaner", "tips",
    "eartips", "earbuds tips", "silicone", "compatible",
}


_ACCESSORY_WORDS = {_stem(w) for w in _ACCESSORY_WORDS_RAW}


def _product_id(item: dict) -> str:
    """Use Google's product_id when present, else a stable hash of the title."""
    if item.get("product_id"):
        return str(item["product_id"])
    title = item.get("title", "").lower().strip()
    return "t_" + hashlib.md5(title.encode()).hexdigest()[:12]


def _significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1}


def _relevance(query_words: set[str], title: str, seller: str = "") -> float:
    """Fraction of the query's significant words found in the title or the seller name."""
    if not query_words:
        return 1.0
    title_words = _significant_words(title)
    seller_words = _significant_words(seller or "")
    seller_squashed = re.sub(r"[^a-z0-9]", "", (seller or "").lower())  # "maxfashion.in" -> "maxfashionin"
    matched = {
        w for w in query_words
        if w in title_words or w in seller_words or (len(w) >= 3 and w in seller_squashed)
    }
    return len(matched) / len(query_words)


def _is_accessory(query_words: set[str], title: str) -> bool:
    """True if this looks like an accessory/part FOR the product, not the product itself.

    Heuristic: the title contains an accessory word (case, cover, strap, etc.)
    that the user did NOT type themselves (so searching "phone case" still
    works normally). If the user searched for the actual device, listings
    for its accessories are treated as a mismatch and filtered out.
    """
    title_words = _significant_words(title)
    accessory_hit = title_words & _ACCESSORY_WORDS
    if not accessory_hit or accessory_hit & query_words:
        # No accessory word, or the user was actually searching for one
        # (e.g. they typed "case" or "cover" themselves) -> not a mismatch.
        return False
    return True


def search_shopping(query: str, limit: int = 20) -> list[dict]:
    """Search Google Shopping (India) via SerpApi and return cleaned, relevance-filtered results."""
    key = os.getenv("SERPAPI_KEY")
    if not key:
        raise RuntimeError("SERPAPI_KEY is not set. Copy .env.example to .env and add your key.")

    params = {
        "engine": "google_shopping",
        "q": query,
        "gl": "in",
        "hl": "en",
        "google_domain": "google.co.in",
        "api_key": key,
    }
    resp = requests.get(SERPAPI_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"SerpApi error: {data['error']}")

    # "earbuds under 1000": use the number as a price cap, not as a word to match in titles.
    max_price = None
    cap = re.search(r"\b(?:under|below|upto|up to)\s*(?:rs\.?|₹)?\s*(\d[\d,]*)", query, re.I)
    if cap:
        max_price = float(cap.group(1).replace(",", ""))
    query_words = _significant_words(re.sub(r"\b(?:under|below|upto|up to)\s*(?:rs\.?|₹)?\s*\d[\d,]*", " ", query, flags=re.I))

    results = []
    for it in data.get("shopping_results", []):
        price = it.get("extracted_price")
        title = it.get("title")
        if price is None or not title:
            continue
        title = re.sub(r"['\"]{2,}", " ", title).strip()  # fixes stray quotes like Top'""by Myntra
        score = _relevance(query_words, title, it.get("source") or "")
        old = it.get("extracted_old_price")
        if old is None or float(old) <= float(price):
            old = None  # a "was" price that isn't higher than the current price is bad data
        results.append(
            {
                "product_id": _product_id(it),
                "title": title,
                "seller": it.get("source"),
                "price": float(price),
                "old_price": old,
                "rating": it.get("rating"),
                "reviews": it.get("reviews"),
                "extensions": it.get("extensions") or [],
                "snippet": it.get("snippet"),
                "delivery": it.get("delivery"),
                "tag": it.get("tag"),
                "link": it.get("product_link") or it.get("link"),
                "thumbnail": it.get("thumbnail"),
                "relevance": round(score, 2),
                "is_accessory": _is_accessory(query_words, title),
            }
        )

    # Filter in two stages, each with a safety fallback so a narrow query
    # never returns an empty page:
    # 1. Drop results that don't plausibly match the query's words at all.
    relevant = [r for r in results if r["relevance"] >= _RELEVANCE_THRESHOLD]
    pool = relevant if relevant else results
    if relevant and len(relevant) < _MIN_RESULTS:
        # Too strict for this query: add the next-best partial matches rather than showing one lonely row.
        rest = sorted(
            (r for r in results if r["relevance"] < _RELEVANCE_THRESHOLD and r["relevance"] > 0),
            key=lambda r: -r["relevance"],
        )
        pool = relevant + rest[: _MIN_RESULTS - len(relevant)]

    # 2. Among relevant results, prefer the actual product over its
    # accessories (cases, straps, chargers...). Only apply this if it still
    # leaves something to show.
    non_accessory = [r for r in pool if not r["is_accessory"]]
    pool = non_accessory if non_accessory else pool

    if max_price is not None:
        capped = [r for r in pool if r["price"] <= max_price]
        pool = capped if capped else pool

    return sorted(pool, key=lambda r: r["price"])[:limit]
