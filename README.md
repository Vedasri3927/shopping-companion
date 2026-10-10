# Shopping Companion

**Is that discount real?** Shopping Companion checks every listing against real price history and gives it a Buy Score, so you can tell a genuine deal from an inflated one.

It uses live Google Shopping (India) data via [SerpApi](https://serpapi.com), records the prices it sees, and compares each listing with that history.

## Features

- **Buy Score (0-100)** for every listing, combining price history, price vs other sellers, star rating, number of reviews, warranty (only if the listing mentions it) and seller trust. Missing data is left out, not guessed.
- **Deal check verdicts** such as *Genuine deal*, *Below typical price*, *Fair price*, *Suspiciously low* and *Not comparable*, each with a one-line reason.
- **Summary bar** with the number of listings, the median price now, the price range, good deals and red flags. If a search is too broad or has too few listings for a fair median, it says so instead of showing a meaningless number.
- **Our pick** card with a direct recommendation and the reason. It never picks a flagged or not-comparable listing, and says "Best available" when the whole set is weak.
- **Price history chart** per product, built from prices the app has recorded, with a note on where today's price sits against its recorded range.
- **Compare** up to 3 listings side by side, with the best value in each row highlighted. A factor with no data shows "n/a", not zero.
- **Known seller** label for established retailers.
- **Wishlist** with a target price and a "Check for price drops" button.
- **"How to read the scores and verdicts"** legend, so the numbers are self-explanatory.
- **Honest empty and thin-result states.** If Google returns only accessories (cases, covers), the app says so instead of recommending one. With very few listings it warns that scores rest on little data.

## How the results are cleaned up

Raw shopping results are noisy, so `serp.py` filters them before scoring:

- relevance filter against the words in your query
- model-code lock (a search for one model drops listings for another)
- brand lock (a search naming a brand drops other brands)
- accessory detection, including common non-English words for "case"
- duplicate removal
- price-outlier removal, so mis-tagged junk can't win "Lowest" or skew the median

## Run

You need Python 3.10+ and a SerpApi key.

**Windows (PowerShell)**
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
```

**macOS / Linux**
```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set `SERPAPI_KEY=your_key_here`, then start the server:

```
uvicorn main:app --reload
```

With the server running, open `index.html` in your browser (double-click it, or run `start index.html` on Windows / `open index.html` on macOS) and search, for example `raga by titan`, `hp victus` or `cmf nothing buds pro 2`. The API docs are at http://127.0.0.1:8000/docs, where you can try `/search?q=boat airdopes 141`.

## Notes

- Repeating a search within 30 minutes loads the saved results (shown as "cached"). Tick **Force live refresh** to fetch new ones.
- Every live search uses one SerpApi credit.
- Price history grows with use: the more often a product is searched, the more reliable its "typical price" becomes. Until then, verdicts say "from similar listings" rather than claiming a history.
- Prices and verdicts are guidance, not financial advice. Always check the seller and model before buying.

## Tech

FastAPI, SQLite, SerpApi (Google Shopping), plain HTML/JS frontend.
