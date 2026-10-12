import hashlib
import os
import re
from collections import Counter

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
# A query word that leads at least this share of the result titles is treated
# as a brand/product-line name (see _infer_brand_tokens).
_BRAND_LEAD_SHARE = 0.3


def _stem(w: str) -> str:
    """Crude plural handling. Words pluralized with "-es" after s/x/ch/sh
    (dresses -> dress, boxes -> box, watches -> watch) need two letters
    stripped, not one, or they'd never match their singular form in titles."""
    if len(w) > 4 and (w.endswith("sses") or w.endswith("xes") or w.endswith("ches") or w.endswith("shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


# ---------------------------------------------------------------- text helpers
# Sellers write "256 GB", "256GB" and "(256gb)" interchangeably, and "20000 mAh"
# as often as "20000mAh". Glue number+unit together so they compare equal, and
# (for titles) also keep the bare number so a query for just "20000" still matches.
_UNITS = "gb|tb|mb|mah|hz|mm|ml|kg|inch|w"
_UNIT_SPACE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + _UNITS + r")\b")
_UNIT_TOKEN_RE = re.compile(r"^(\d+)(" + _UNITS + r")$")
_TSHIRT_RE = re.compile(r"\bt[\s-]+shirts?\b")  # "T-Shirt", "T Shirts" -> "tshirt"


def _normalize(text: str) -> str:
    text = _TSHIRT_RE.sub("tshirt", text.lower())
    return _UNIT_SPACE_RE.sub(r"\1\2", text)


def _ordered_words(text: str, expand_units: bool = False) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", _normalize(text)):
        # Drop stray single letters, but keep single digits: they are real model numbers
        # ("series 9", "phone 2", "buds pro 2") and dropping them lets other models through.
        if w in _STOPWORDS or (len(w) <= 1 and not w.isdigit()):
            continue
        out.append(_stem(w))
        if expand_units:
            m = _UNIT_TOKEN_RE.match(w)
            if m and len(m.group(1)) > 1:
                out.append(m.group(1))
    return out


def _significant_words(text: str, expand_units: bool = False) -> set[str]:
    return set(_ordered_words(text, expand_units))


# ---------------------------------------------------------------- word lists
# Accessory vocabulary. These words are only treated as "this listing is an
# accessory" for device-type searches (see _DEVICE_HINTS) -- in apparel, "sleeve",
# "wrap", "belt" and "strap" are ordinary product features, not accessories.
# Words that are also ordinary spec/feature words on real devices ("screen",
# "dust" as in dust-resistant, "kit" as in a lens kit, "compatible") are NOT here;
# "made for the product you searched" is caught by _is_accessory's designed-for rule.
_ACCESSORY_WORDS_RAW = {
    "case", "cover", "skin", "strap", "sticker", "protector", "pouch",
    "holder", "charger", "cable", "adapter", "mount", "tempered",
    "screenguard", "band", "belt", "pendrive", "spare",
    "replacement", "repair", "cleaning", "cleaner", "tips",
    "eartips", "silicone",
    "bumper", "sleeve", "decal", "guard", "popsocket", "grip",
    "stand", "organizer", "organiser",
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
    "canon", "nikon", "gopro", "miniso",
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

# Words that mark a search as being for an electronic device. Accessory words
# are only applied to such searches.
_DEVICE_HINTS_RAW = {
    "phone", "phones", "mobile", "smartphone", "laptop", "laptops", "notebook", "tablet",
    "earbuds", "earbud", "earphone", "earphones", "headphone", "headphones", "neckband",
    "speaker", "soundbar", "tv", "television", "monitor", "keyboard", "mouse",
    "camera", "dslr", "drone", "router", "printer", "bank", "powerbank", "console",
    "watch", "smartwatch", "smartband", "airdopes", "buds",
    "asus", "hp", "dell", "lenovo", "acer", "apple", "samsung", "msi", "lg", "sony",
    "boat", "oneplus", "xiaomi", "redmi", "realme", "oppo", "vivo", "nothing", "jbl",
    "sennheiser", "bose", "infinix", "tecno", "iqoo", "motorola", "nokia", "honor",
    "google", "pixel", "micromax", "lava", "itel", "gigabyte", "zebronics", "mi",
    "iphone", "galaxy", "macbook", "ipad", "airpods", "canon", "nikon", "gopro",
}
_DEVICE_HINTS = {_stem(w) for w in _DEVICE_HINTS_RAW}

# Sellers that exclusively sell phone/device accessories. A listing from one
# of these is almost never the actual device, even if its product-line name
# (e.g. "SolidX", "Mod NX") doesn't contain an obvious word like "case".
# (ubuy is a general marketplace that sells real laptops and phones too, so it
# is deliberately not on this list.)
_ACCESSORY_ONLY_SELLERS = {
    "rhinoshield", "rhinoshield.io", "spigen", "otterbox", "caseology",
    "ringke", "casetify", "case-mate", "casemate",
}

_BUNDLE_BEFORE = {"with", "without", "includes", "including", "free", "plus", "+", "&"}
_DESIGNED_FOR_CUES = {"compatible", "for", "fits"}
_WATCH_WORDS = {"watch", "smartwatch"}
_WEARABLE_PARTS = {"strap", "band", "case"}  # features of a watch, not accessories, in a watch search


def _product_id(item: dict) -> str:
    if item.get("product_id"):
        return str(item["product_id"])
    title = item.get("title", "").lower().strip()
    return "t_" + hashlib.md5(title.encode()).hexdigest()[:12]


def _relevance(query_words: set[str], title: str, seller: str = "") -> float:
    if not query_words:
        return 1.0
    title_words = _significant_words(title, expand_units=True)
    seller_words = _significant_words(seller or "")
    seller_squashed = re.sub(r"[^a-z0-9]", "", (seller or "").lower())
    matched = {
        w for w in query_words
        if w in title_words or w in seller_words or (len(w) >= 3 and w in seller_squashed)
    }
    return len(matched) / len(query_words)


def _is_accessory(query_words: set[str], title: str, seller: str = "") -> bool:
    """True if this looks like an accessory/part FOR the product, not the product itself.

    Three signals, from most to least specific:
    1. The seller only sells accessories.
    2. Designed-for phrasing: "<thing> for / compatible with <the product searched>"
       (a case "for iPhone 15", a charger "compatible with Canon EOS 1500D").
    3. (Device searches only) an accessory word such as case/cover/strap, unless it
       is clearly part of what's being sold: "with charger", "charger included",
       "charging case" in a bundle, or a watch's own strap/band/case.
    """
    seller_squashed = re.sub(r"[^a-z0-9.]", "", (seller or "").lower())
    if any(s in seller_squashed for s in _ACCESSORY_ONLY_SELLERS):
        return True

    toks = re.findall(r"[a-z0-9]+|[+&]", _normalize(title))
    stems = [_stem(t) for t in toks]
    has_device = bool(query_words & _DEVICE_HINTS)
    has_model = any(any(c.isdigit() for c in w) for w in query_words)

    if has_device or has_model:
        need = min(2, len(query_words))
        if need:
            for i, t in enumerate(toks):
                if t in _DESIGNED_FOR_CUES and len(set(stems[i + 1:i + 6]) & query_words) >= need:
                    return True

    if not has_device:
        return False

    watchy = bool(query_words & _WATCH_WORDS) and bool(set(stems) & _WATCH_WORDS)
    for i, st in enumerate(stems):
        if st not in _ACCESSORY_WORDS or st in query_words:
            continue
        if watchy and st in _WEARABLE_PARTS:
            continue
        if _BUNDLE_BEFORE & set(toks[max(0, i - 4):i]):
            continue
        after = toks[i + 1:i + 4]
        if "included" in after or "free" in after or after[:2] == ["in", "box"] or after[:3] == ["in", "the", "box"]:
            continue
        return True
    return False


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


def _infer_brand_tokens(query_words: set[str], titles: list[str], known: set[str]) -> set[str]:
    """Brands we have no list entry for (ambrane, noise, fire-boltt...).

    A query word that starts the title of a decent share of the results is almost
    certainly a brand or product-line name -- real listings lead with the brand.
    Generic descriptors ("relaxed", "gaming") and numbers are never inferred.
    """
    if len(titles) < 5:
        return set()
    cands = {
        w for w in query_words
        if w not in known and w not in _CATEGORY_WORDS and len(w) >= 3 and not any(c.isdigit() for c in w)
    }
    if not cands:
        return set()
    leads = Counter()
    for t in titles:
        for w in set(_ordered_words(t)[:2]) & cands:
            leads[w] += 1
    return {w for w, c in leads.items() if c / len(titles) >= _BRAND_LEAD_SHARE}


def _brand_match(brand_tokens: set[str], title_words: set[str], seller: str) -> bool:
    if not brand_tokens:
        return True
    if brand_tokens & title_words:
        return True
    # Seller names count too (an official "boAt Lifestyle" store whose titles omit the
    # brand), but short brand names must match a whole word, or "mi" would match
    # a seller called "Premium Gadgets".
    seller_squashed = re.sub(r"[^a-z0-9]", "", (seller or "").lower())
    seller_words = {_stem(w) for w in re.findall(r"[a-z0-9]+", (seller or "").lower())}
    return any(b in seller_words or (len(b) >= 4 and b in seller_squashed) for b in brand_tokens)


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


def search_shopping(query: str, limit: int = 20, trace: list | None = None) -> list[dict]:
    """Search Google Shopping (India). Pass an empty list as `trace` to also get an explanation
    of what was returned and why each listing was kept or dropped (see debug_search.py)."""
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

    # Brands that aren't in our list: infer them from how the results are titled.
    # Used only for the brand lock; model_tokens above still uses the known list,
    # so a collection name like "raga" stays a required word.
    all_titles = [
        re.sub(r"['\"]{2,}", " ", it.get("title") or "").strip()
        for it in data.get("shopping_results", [])
        if it.get("extracted_price") is not None and it.get("title")
    ]
    match_brands = brand_tokens | _infer_brand_tokens(query_words, all_titles, brand_tokens)

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
        title_words = _significant_words(title, expand_units=True)
        # ALL named model/collection words must appear, not just one -- if
        # someone named both "tuf" and "a15", a listing with only "tuf" is
        # a different model (e.g. the A18) and should NOT pass.
        model_match = (not model_tokens) or model_tokens.issubset(title_words)
        brand_match = _brand_match(match_brands, title_words, seller)
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
    if match_brands:
        brand_ok = [r for r in pool if r["brand_match"]]
        pool = brand_ok if (brand_ok or not (brand_tokens and all(len(x) >= 4 for x in brand_tokens))) else []

    # 3. Prefer the actual product over its accessories.
    non_accessory = [r for r in pool if not r["is_accessory"]]
    pool = non_accessory if non_accessory else pool

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
        if match_brands:
            candidates = (r for r in candidates if r["brand_match"])
        # Never top up with accessories: a case that mentions the product is not the product.
        candidates = (r for r in candidates if not r["is_accessory"])
        backfill = sorted(candidates, key=lambda r: -r["relevance"])
        pool = pool + backfill[: _MIN_RESULTS - len(pool)]

    final = sorted(pool, key=lambda r: r["price"])[:limit]

    if trace is not None:
        kept = {r["product_id"] for r in final}
        rows = []
        for r in sorted(results, key=lambda r: r["price"]):
            flags = []
            if r["relevance"] < _RELEVANCE_THRESHOLD:
                flags.append(f"low relevance {r['relevance']}")
            if model_tokens and not r["model_match"]:
                missing = model_tokens - _significant_words(r["title"], expand_units=True)
                flags.append("missing model word(s): " + ", ".join(sorted(missing)))
            if match_brands and not r["brand_match"]:
                flags.append("different brand")
            if r["is_accessory"]:
                flags.append("looks like an accessory")
            if max_price is not None and r["price"] > max_price:
                flags.append("above price cap")
            rows.append({"kept": r["product_id"] in kept, "price": r["price"], "seller": r["seller"],
                         "title": r["title"], "flags": flags})
        trace.append({
            "query": query,
            "raw_listings": len(data.get("shopping_results", [])),
            "with_price_and_title": len(results),
            "query_words": sorted(query_words), "known_brands": sorted(brand_tokens),
            "inferred_brands": sorted(match_brands - brand_tokens), "required_model_words": sorted(model_tokens),
            "kept": len(final), "rows": rows,
        })
    return final
