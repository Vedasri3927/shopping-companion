"""Show what Google Shopping returned for a search and why each listing was kept or dropped.

    python debug_search.py boat airdopes 141

Uses one SerpApi credit (a live search) and changes nothing in your database.
"""
import sys

import serp


def main(argv):
    try:  # Windows consoles can choke on unusual characters in titles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    query = " ".join(argv).strip() or "boat airdopes 141"
    trace = []
    serp.search_shopping(query, trace=trace)
    t = trace[0]
    print(f"\nSearch: {t['query']!r}")
    print(f"Google returned {t['raw_listings']} listings ({t['with_price_and_title']} with a price and title). Kept: {t['kept']}")
    print(f"Words searched for : {', '.join(t['query_words'])}")
    print(f"Known brand        : {', '.join(t['known_brands']) or '-'}")
    print(f"Inferred brand     : {', '.join(t['inferred_brands']) or '-'}")
    print(f"Required model word: {', '.join(t['required_model_words']) or '-'}\n")
    for r in t["rows"]:
        mark = "KEPT   " if r["kept"] else "dropped"
        why = "" if r["kept"] else "  <- " + ("; ".join(r["flags"]) or "removed as a duplicate or price outlier")
        print(f"{mark}  Rs {r['price']:>9,.0f}  {str(r['seller'] or '')[:20]:20}  {r['title'][:58]}{why}")
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
