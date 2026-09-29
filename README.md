# Shopping Companion

"Should I buy this now?" Live Google Shopping (India) data via SerpApi, with price history tracking.

## Run
```
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add your SERPAPI_KEY
uvicorn main:app --reload
```
Open http://127.0.0.1:8000/docs and try `/search?q=boat airdopes 141`.
