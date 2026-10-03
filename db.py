import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "shopping.db"
CACHE_MINUTES = 30  # reuse recent results for the same query to save SerpApi credits


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                thumbnail TEXT,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL REFERENCES products(id),
                seller TEXT,
                price REAL NOT NULL,
                old_price REAL,
                rating REAL,
                reviews INTEGER,
                link TEXT,
                query TEXT,
                captured_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_snap_product_time
                ON price_snapshots(product_id, captured_at);
            CREATE INDEX IF NOT EXISTS idx_snap_query_time
                ON price_snapshots(query, captured_at);

            CREATE TABLE IF NOT EXISTS wishlist (
                product_id TEXT PRIMARY KEY REFERENCES products(id),
                target_price REAL,
                added_price REAL,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def save_results(query: str, items: list[dict]) -> int:
    """Store products and one price snapshot per result. Returns snapshots saved."""
    with get_conn() as conn:
        for it in items:
            conn.execute(
                "INSERT OR IGNORE INTO products (id, title, thumbnail) VALUES (?, ?, ?)",
                (it["product_id"], it["title"], it.get("thumbnail")),
            )
            conn.execute(
                """INSERT INTO price_snapshots
                   (product_id, seller, price, old_price, rating, reviews, link, query)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    it["product_id"],
                    it.get("seller"),
                    it["price"],
                    it.get("old_price"),
                    it.get("rating"),
                    it.get("reviews"),
                    it.get("link"),
                    query,
                ),
            )
    return len(items)


def get_cached(query: str) -> list[dict]:
    """Latest snapshots for this query if captured within CACHE_MINUTES."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT p.id AS product_id, p.title, p.thumbnail, s.seller, s.price,
                       s.old_price, s.rating, s.reviews, s.link
                FROM price_snapshots s JOIN products p ON p.id = s.product_id
                WHERE s.query = ?
                  AND s.captured_at >= datetime('now', '-{CACHE_MINUTES} minutes')
                  AND s.captured_at = (SELECT MAX(captured_at) FROM price_snapshots
                                       WHERE query = ?)
                ORDER BY s.price ASC""",
            (query, query),
        ).fetchall()
    return [dict(r) for r in rows]


def get_history(product_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT price, seller, captured_at FROM price_snapshots
               WHERE product_id = ? ORDER BY captured_at ASC""",
            (product_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- wishlist

def _current_price(conn, product_id: str):
    """Cheapest price from the product's most recent capture (rows within 1 minute of the latest)."""
    return conn.execute(
        """SELECT price, seller, link, query FROM price_snapshots
           WHERE product_id = ?
             AND captured_at >= datetime((SELECT MAX(captured_at) FROM price_snapshots
                                          WHERE product_id = ?), '-1 minute')
           ORDER BY price ASC LIMIT 1""",
        (product_id, product_id),
    ).fetchone()


def add_wishlist(product_id: str, target_price: float | None = None) -> None:
    """Add a product, or update its target if already saved. Raises ValueError if never searched."""
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
            raise ValueError("Unknown product - search for it first")
        if conn.execute("SELECT 1 FROM wishlist WHERE product_id = ?", (product_id,)).fetchone():
            conn.execute(
                "UPDATE wishlist SET target_price = ? WHERE product_id = ?",
                (target_price, product_id),
            )
            return
        cur = _current_price(conn, product_id)
        conn.execute(
            "INSERT INTO wishlist (product_id, target_price, added_price) VALUES (?, ?, ?)",
            (product_id, target_price, cur["price"] if cur else None),
        )


def remove_wishlist(product_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM wishlist WHERE product_id = ?", (product_id,))
    return cur.rowcount > 0


def get_wishlist() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT w.product_id, w.target_price, w.added_price, w.added_at,
                      p.title, p.thumbnail
               FROM wishlist w JOIN products p ON p.id = w.product_id
               ORDER BY w.added_at DESC"""
        ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            cur = _current_price(conn, r["product_id"])
            lowest = conn.execute(
                "SELECT MIN(price) FROM price_snapshots WHERE product_id = ?",
                (r["product_id"],),
            ).fetchone()[0]
            now = cur["price"] if cur else None
            item["current_price"] = now
            item["seller"] = cur["seller"] if cur else None
            item["link"] = cur["link"] if cur else None
            item["query"] = cur["query"] if cur else None
            item["lowest_seen"] = lowest
            target, added = item["target_price"], item["added_price"]
            item["target_hit"] = bool(target is not None and now is not None and now <= target)
            item["dropped_by"] = round(added - now, 2) if (added and now and now < added) else 0
            items.append(item)
    return items
