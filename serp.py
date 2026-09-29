import hashlib
import os

import requests
from dotenv import load_dotenv

load_dotenv()

SERPAPI_URL = "https://serpapi.com/search.json"


def _product_id(item: dict) -> str:
    """Use Google's product_id when present, else a stable hash of the title."""
    if item.get("product_id"):
        return str(item["product_id"])
    title = item.get("title", "").lower().strip()
    return "t_" + hashlib.md5(title.encode()).hexdigest()[:12]


def search_shopping(query: str, limit: int = 20) -> list[dict]:
    """Search Google Shopping (India) via SerpApi and return cleaned results."""
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

    results = []
    for it in data.get("shopping_results", [])[:limit]:
        price = it.get("extracted_price")
        if price is None or not it.get("title"):
            continue
        results.append(
            {
                "product_id": _product_id(it),
                "title": it["title"].strip(),
                "seller": it.get("source"),
                "price": float(price),
                "old_price": it.get("extracted_old_price"),
                "rating": it.get("rating"),
                "reviews": it.get("reviews"),
                "link": it.get("product_link") or it.get("link"),
                "thumbnail": it.get("thumbnail"),
            }
        )
    return sorted(results, key=lambda r: r["price"])
