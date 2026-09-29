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
