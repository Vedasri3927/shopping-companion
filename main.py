from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from db import get_cached, get_history, init_db, save_results
from serp import search_shopping

app = FastAPI(title="Shopping Companion")
init_db()

# Allows the static frontend (opened as a local file or served separately) to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/search")
def search(q: str = Query(..., min_length=2), refresh: bool = False):
    """Search products. Live results are saved as price snapshots."""
    if not refresh:
        cached = get_cached(q)
        if cached:
            return {"query": q, "source": "cache", "count": len(cached), "results": cached}
    try:
        items = search_shopping(q)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    save_results(q, items)
    return {"query": q, "source": "live", "count": len(items), "results": items}


@app.get("/history/{product_id}")
def history(product_id: str):
    """All stored price snapshots for a product (feeds the Day 4 chart)."""
    rows = get_history(product_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No history for this product yet")
    return {"product_id": product_id, "snapshots": rows}
