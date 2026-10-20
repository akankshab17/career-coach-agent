# Career Coach Research Agent (Step 1)

Searches the web (Tavily) and writes a cited interview-prep brief (Groq or OpenAI, switchable).

## Setup
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TAVILY_API_KEY and GROQ_API_KEY
```

## Choose an LLM provider
Set these in `.env` (both optional; default is Groq / gpt-oss-120b):
```
LLM_PROVIDER=groq      # or: openai
LLM_MODEL=             # blank = provider default (groq: openai/gpt-oss-120b, openai: gpt-4o-mini)
```
OpenAI needs `OPENAI_API_KEY`; Groq needs `GROQ_API_KEY`. To see which models your key can use right now (e.g. if a default gets retired):
```
python src/llm.py models          # uses LLM_PROVIDER
python src/llm.py models openai
```
Each run prints a one-line summary
(provider, model, tokens, latency, estimated cost) to stderr. Prices live in `PRICES` in `src/llm.py`;
a model with no entry shows cost as n/a. Rate limits/timeouts are retried twice with backoff, then a clear error is shown.

## Run
```
python src/main.py --company "Stripe" --role "Backend Engineer" --level new-grad
python src/main.py --company "Stripe" --role "Backend Engineer" --jd "$(cat jd.txt)" -o brief.md
```

## Test
```
pytest
```

`--level` is `intern`, `new-grad` or `experienced`; the default is `new-grad`. Section 3 is based on the single posting that matches it.

Every sentence in the brief cites a source from the search results; a second LLM pass checks each sentence against its source and drops unsupported ones (the count is logged). Claims from Reddit/forums are phrased as "candidates report...".
Only search APIs are used — no scraping.
