from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from db import (
    add_wishlist,
    get_cached,
    get_history,
    get_wishlist,
    init_db,
    remove_wishlist,
    save_results,
)
from scoring import score_results
from serp import search_shopping

app = FastAPI(title="Shopping Companion")
init_db()

# Allows the static frontend (opened as a local file or served separately) to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


class WishlistIn(BaseModel):
    product_id: str
    target_price: Optional[float] = Field(default=None, gt=0)


@app.get("/search")
def search(q: str = Query(..., min_length=2), refresh: bool = False):
    """Search products. Live results are saved as price snapshots."""
    if not refresh:
        cached = get_cached(q)
        if cached:
            score_results(q, cached, record=False)  # cache hits must not add duplicate history
            return {"query": q, "source": "cache", "count": len(cached), "results": cached}
    try:
        items = search_shopping(q)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    save_results(q, items)
    score_results(q, items)  # adds buy_score, deal_verdict, deal_reason, score_breakdown
    return {"query": q, "source": "live", "count": len(items), "results": items}


@app.get("/history/{product_id}")
def history(product_id: str):
    """All stored price snapshots for a product (feeds the Day 4 chart)."""
    rows = get_history(product_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No history for this product yet")
    return {"product_id": product_id, "snapshots": rows}


@app.get("/wishlist")
def wishlist():
    """Saved products with current price, target and price-drop flags."""
    items = get_wishlist()
    return {"count": len(items), "items": items}


@app.post("/wishlist")
def wishlist_add(body: WishlistIn):
    """Save a product (or update its target price). Product must have been searched before."""
    try:
        add_wishlist(body.product_id, body.target_price)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/wishlist/{product_id}")
def wishlist_remove(product_id: str):
    if not remove_wishlist(product_id):
        raise HTTPException(status_code=404, detail="Not in wishlist")
    return {"ok": True}
