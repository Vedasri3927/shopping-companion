import hashlib
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

SERPAPI_URL = "https://serpapi.com/search.json"

_STOPWORDS = {
    "for", "with", "the", "a", "an", "and", "or", "of", "in", "on", "by",
    "men", "women", "kids", "new", "best", "buy",
    "size", "xs", "xl", "xxl", "small", "medium", "large",
    "under", "below", "above", "upto", "price", "online", "india", "cheap",
}
_RELEVANCE_THRESHOLD = 0.5
_MIN_RESULTS = 5
# Listings priced below this fraction of the pool's median are treated as
# mis-tagged/junk (e.g. a ₹2 "accessory" bundled into a phone search) and
# dropped entirely, so they can't win "Lowest" or distort the typical price.
_OUTLIER_FLOOR = 0.2


def _stem(w: str) -> str:
    """Crude plural handling. Words pluralized with "-es" after s/x/ch/sh
    (dresses -> dress, boxes -> box, watches -> watch) need two letters
    stripped, not one, or they'd never match their singular form in titles."""
    if len(w) > 4 and (w.endswith("sses") or w.endswith("xes") or w.endswith("ches") or w.endswith("shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


_ACCESSORY_WORDS_RAW = {
    "case", "cover", "skin", "strap", "sticker", "protector", "pouch",
    "holder", "charger", "cable", "adapter", "mount", "tempered",
    "screenguard", "screen", "band", "belt", "pendrive", "spare",
    "replacement", "repair", "kit", "cleaning", "cleaner", "tips",
    "eartips", "earbuds tips", "silicone", "compatible",
    # added: common accessory words that slipped through before
    "bumper", "sleeve", "decal", "guard", "wrap", "pop", "grip",
    "stand", "organizer", "organiser", "dust",
}
_ACCESSORY_WORDS = {_stem(w) for w in _ACCESSORY_WORDS_RAW}

# If the query names one of these brands, a result must mention that same
# brand (in title or seller) to count as a match -- stops a same-category
# competitor (e.g. "HP Victus" for an "asus gaming laptop" search) from
# passing just because it shares generic words like "gaming laptop".
_BRAND_WORDS_RAW = {
    "asus", "hp", "dell", "lenovo", "acer", "apple", "samsung", "msi", "lg",
    "sony", "boat", "oneplus", "xiaomi", "redmi", "realme", "oppo", "vivo",
    "nothing", "jbl", "sennheiser", "bose", "nike", "adidas", "puma",
    "reebok", "levis", "zara", "titan", "fossil", "casio", "whirlpool",
    "haier", "godrej", "bajaj", "philips", "panasonic", "infinix", "tecno",
    "iqoo", "motorola", "nokia", "honor", "google", "pixel", "micromax",
    "lava", "itel", "mi", "gigabyte", "hcl", "zebronics", "croma", "toshiba",
    # product-line words that function as a de-facto brand/identity in how
    # people actually search, even though they're not the company name --
    # "iphone" implies Apple, and typing it should lock out other brands
    # AND (via the word-based model lock) require exact variant words like
    # "pro"/"max" to actually be present, not just "iphone 18".
    "iphone", "galaxy", "macbook", "ipad", "airpods",
}
_BRAND_WORDS = {_stem(w) for w in _BRAND_WORDS_RAW}

# Generic category/descriptor words that should NOT be treated as a specific
# model/collection name, even when they survive to this point. Without this
# list, a word-based model-lock would also force words like "gaming" or
# "wireless" to be mandatory, which is too strict for how real titles vary.
_CATEGORY_WORDS_RAW = {
    "watch", "watches", "laptop", "laptops", "phone", "phones", "mobile",
    "smartphone", "earbuds", "earbud", "earphone", "earphones", "headphone",
    "headphones", "shoe", "shoes", "sneaker", "sneakers", "jean", "jeans",
    "dress", "dresses", "pant", "pants", "trouser", "trousers", "shirt",
    "shirts", "tshirt", "tee", "jacket", "jackets", "bag", "bags",
    "backpack", "wallet", "ring", "necklace", "bracelet", "laptop",
    "gaming", "wireless", "bluetooth", "smart", "analog", "digital",
    "quartz", "automatic", "casual", "formal", "sports", "running",
    "cotton", "leather", "regular", "slim", "loose", "relaxed", "fit",
    "style", "stylish", "trendy", "fashion", "edition", "series",
    "collection", "set", "pack", "combo",
}
_CATEGORY_WORDS = {_stem(w) for w in _CATEGORY_WORDS_RAW}

# Sellers that exclusively sell phone/device accessories. A listing from one
# of these is almost never the actual device, even if its product-line name
# (e.g. "SolidX", "Mod NX") doesn't contain an obvious word like "case".
_ACCESSORY_ONLY_SELLERS = {
    "rhinoshield", "rhinoshield.io", "spigen", "otterbox", "caseology",
    "ringke", "casetify", "case-mate", "casemate",
}


_STRONG_ACCESSORY_RAW = {
    "etui", "funda", "coque", "custodia", "capa", "estuche",
    "case", "cover", "skin", "sticker", "protector", "pouch", "strap",
    "charger", "cable", "adapter", "tempered", "screenguard", "decal",
    "bumper", "sleeve", "tips", "eartips", "holder", "mount",
}
_STRONG_ACCESSORY_WORDS = {_stem(w) for w in _STRONG_ACCESSORY_RAW}


def _is_strong_accessory(query_words: set[str], title: str, seller: str = "") -> bool:
    """Clearly a case/cover/charger/etc. (not a vague word like 'screen' or 'stand')."""
    seller_squashed = re.sub(r"[^a-z0-9.]", "", (seller or "").lower())
    if any(s in seller_squashed for s in _ACCESSORY_ONLY_SELLERS):
        return True
    hit = _significant_words(title) & _STRONG_ACCESSORY_WORDS
    return bool(hit) and not (hit & query_words)


def _product_id(item: dict) -> str:
    if item.get("product_id"):
        return str(item["product_id"])
    title = item.get("title", "").lower().strip()
    return "t_" + hashlib.md5(title.encode()).hexdigest()[:12]


def _significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", re.sub(r"['’`]", "", text.lower()))
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1}


def _relevance(query_words: set[str], title: str, seller: str = "") -> float:
    if not query_words:
        return 1.0
    title_words = _significant_words(title)
    seller_words = _significant_words(seller or "")
    seller_squashed = re.sub(r"[^a-z0-9]", "", (seller or "").lower())
    matched = {
        w for w in query_words
        if w in title_words or w in seller_words or (len(w) >= 3 and w in seller_squashed)
    }
    return len(matched) / len(query_words)


def _is_accessory(query_words: set[str], title: str, seller: str = "") -> bool:
    """True if this looks like an accessory/part FOR the product, not the product itself."""
    seller_squashed = re.sub(r"[^a-z0-9.]", "", (seller or "").lower())
    if any(s in seller_squashed for s in _ACCESSORY_ONLY_SELLERS):
        return True
    title_words = _significant_words(title)
    accessory_hit = title_words & _ACCESSORY_WORDS
    if not accessory_hit or accessory_hit & query_words:
        return False
    return True


def _model_tokens(query_words: set[str], brand_tokens: set[str]) -> set[str]:
    """Words that likely name a specific model/collection rather than
    describing the product generically.

    Always included: digit-containing codes like 'a15' or '141' -- these
    are unambiguous regardless of whether a brand was named.

    Only when a brand WAS named: any other leftover word that isn't the
    brand itself and isn't a generic category/descriptor (e.g. 'raga' in
    'raga by titan', or 'tuf' in 'asus tuf a15'). This is scoped to
    brand-qualified queries on purpose -- applying it to a plain descriptive
    search like 'relaxed fit jeans for men' (no brand named) would wrongly
    force every one of those adjectives to appear verbatim in the title.
    """
    digits = {w for w in query_words if any(ch.isdigit() for ch in w)}
    extra = set()
    if brand_tokens:
        extra = {
            w for w in query_words
            if w not in brand_tokens and w not in _CATEGORY_WORDS and w not in digits
        }
    return digits | extra


def _brand_tokens(query_words: set[str]) -> set[str]:
    """Known brand name(s) the user typed, if any."""
    return query_words & _BRAND_WORDS


def _brand_match(brand_tokens: set[str], title_words: set[str], seller: str) -> bool:
    if not brand_tokens:
        return True
    seller_squashed = re.sub(r"[^a-z0-9]", "", (seller or "").lower())
    return bool(brand_tokens & title_words) or any(b in seller_squashed for b in brand_tokens)


def _dedupe(results: list[dict]) -> list[dict]:
    """Drop repeat listings SerpApi sometimes returns for the same product/seller/price."""
    seen = set()
    deduped = []
    for r in results:
        key = (r["title"].strip().lower(), (r["seller"] or "").strip().lower(), r["price"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def _drop_price_outliers(pool: list[dict]) -> list[dict]:
    """Remove listings priced far below the group's median (almost always a
    mis-tagged accessory/sticker, not a real deal on the product itself)."""
    if len(pool) < 4:
        return pool
    prices = sorted(r["price"] for r in pool)
    mid = len(prices) // 2
    median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
    floor = median * _OUTLIER_FLOOR
    kept = [r for r in pool if r["price"] >= floor]
    return kept if kept else pool


def search_shopping(query: str, limit: int = 20) -> list[dict]:
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

    max_price = None
    cap = re.search(r"\b(?:under|below|upto|up to)\s*(?:rs\.?|₹)?\s*(\d[\d,]*)", query, re.I)
    if cap:
        max_price = float(cap.group(1).replace(",", ""))
    query_words = _significant_words(re.sub(r"\b(?:under|below|upto|up to)\s*(?:rs\.?|₹)?\s*\d[\d,]*", " ", query, flags=re.I))
    brand_tokens = _brand_tokens(query_words)
    model_tokens = _model_tokens(query_words, brand_tokens)

    results = []
    for it in data.get("shopping_results", []):
        price = it.get("extracted_price")
        title = it.get("title")
        if price is None or not title:
            continue
        title = re.sub(r"['\"]{2,}", " ", title).strip()
        seller = it.get("source") or ""
        score = _relevance(query_words, title, seller)
        old = it.get("extracted_old_price")
        if old is None or float(old) <= float(price):
            old = None
        title_words = _significant_words(title)
        # ALL named model/collection words must appear, not just one -- if
        # someone named both "tuf" and "a15", a listing with only "tuf" is
        # a different model (e.g. the A18) and should NOT pass.
        model_match = (not model_tokens) or model_tokens.issubset(title_words)
        brand_match = _brand_match(brand_tokens, title_words, seller)
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
                "is_accessory": _is_accessory(query_words, title, seller),
                "strong_accessory": _is_strong_accessory(query_words, title, seller),
                "model_match": model_match,
                "brand_match": brand_match,
            }
        )

    # 1. Relevance filter, with fallback so a narrow query never returns empty.
    relevant = [r for r in results if r["relevance"] >= _RELEVANCE_THRESHOLD]
    pool = relevant if relevant else results
    if relevant and len(relevant) < _MIN_RESULTS:
        rest = sorted(
            (r for r in results if r["relevance"] < _RELEVANCE_THRESHOLD and r["relevance"] > 0),
            key=lambda r: -r["relevance"],
        )
        pool = relevant + rest[: _MIN_RESULTS - len(relevant)]

    # 2. Model-code lock: if the query named a specific model, drop listings
    # for a different model, even if they otherwise share enough words.
    if model_tokens:
        model_ok = [r for r in pool if r["model_match"]]
        pool = model_ok if model_ok else pool

    # 2b. Brand lock: if the query named a specific brand, drop listings for
    # a different/unnamed brand, even if they share generic words like
    # "gaming laptop" or "wireless earbuds".
    if brand_tokens:
        brand_ok = [r for r in pool if r["brand_match"]]
        pool = brand_ok if brand_ok else pool

    # 3. Prefer the actual product over its accessories.
    non_accessory = [r for r in pool if not r["is_accessory"]]
    pool = non_accessory if non_accessory else [r for r in pool if not r["strong_accessory"]]

    if max_price is not None:
        capped = [r for r in pool if r["price"] <= max_price]
        pool = capped if capped else pool

    # 4. Collapse exact repeat listings.
    pool = _dedupe(pool)

    # 5. Drop price outliers (mis-tagged junk) so they can't win "Lowest" or
    # skew the median the Buy Score treats as the "typical price".
    pool = _drop_price_outliers(pool)

    # 6. Safety net: each stage above only backs off when IT would empty the
    # page, but several moderate trims in a row can still collapse a healthy
    # 20-result search down to a handful (e.g. one seller's listings surviving
    # every filter while everyone else's get trimmed a little at each stage).
    # If that's happened, top back up from the full candidate pool -- by
    # relevance -- so the page always shows a reasonable spread rather than
    # a tiny, accidentally one-sided list.
    #
    # IMPORTANT: this must still respect the brand/model lock. Those aren't
    # an accidental side-effect of stacking filters -- they're a deliberate
    # "this is a different product" decision (e.g. excluding a Titan Neo
    # watch from a "raga by titan" search). Backfilling past that would
    # silently undo the fix and let the wrong product back in.
    if len(pool) < _MIN_RESULTS:
        have = {r["product_id"] for r in pool}
        candidates = (r for r in results if r["product_id"] not in have)
        if model_tokens:
            candidates = (r for r in candidates if r["model_match"])
        if brand_tokens:
            candidates = (r for r in candidates if r["brand_match"])
        # Never top up with accessories: a case that mentions the product is not the product.
        candidates = (r for r in candidates if not r["is_accessory"])
        backfill = sorted(candidates, key=lambda r: -r["relevance"])
        pool = pool + backfill[: _MIN_RESULTS - len(pool)]

    return sorted(pool, key=lambda r: r["price"])[:limit]
