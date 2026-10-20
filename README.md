# Career Coach Research Agent

Searches the web (Tavily) and writes a cited interview-prep brief (Groq or OpenAI, switchable).

## Setup
```
python3.12 -m venv .venv && source .venv/bin/activate   # Python 3.10+ (LangGraph)
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

`--level` is `intern`, `new-grad` or `experienced`; the default is `new-grad`. Postings at other levels are ignored.

## How it works: extract, then write
The pipeline is a LangGraph graph (`src/graph.py`):

```
search -> fetch -> extract -> ground -> coverage --enough--> write -> finalize
            ^                              |
            +------ search_more <--too few-+   (at most once)
```

1. **search**: Tavily search for company news, interview experiences (incl. Reddit) and the posting at your level.
2. **fetch**: full page text (Tavily extract) for the posting, every interview/forum source, and the top 3 company sources.
   Link/image markup is stripped before the 6,000-char cap (forum pages start with thousands of chars of nav links).
   Pages that can't be fetched (Reddit, usually) fall back to their search snippet, which is still real source text.
   glassdoor.* is blocked everywhere (never searched, extracted or cited).
   Interview results are filtered for relevance: the company must be in the title or URL, and the role family
   (software, hardware, data, product...) must match, so an HPE post or a "Silicon/FPGA intern" post can't stand in for
   "Google Software Engineering Intern". What was filtered is logged and listed in the debug file. Stripe `/careers/apply/` URLs become `/careers/listing/`; if a posting page
   can't be extracted (or is under 500 chars) the next-best posting at your level is tried.
3. **extract**: one LLM call per source (full page or snippet) returns facts as `{fact, exact_quote, section, stage, is_forum}`.
   `stage` (interview facts): `oa`, `phone-screen`, `team-screen`, `onsite` or `unknown`.
4. **ground** (code, no LLM): a fact is kept only if its `exact_quote` appears in the page text or snippet (ignoring
   case, whitespace, punctuation, curly quotes, dashes, ellipses and markdown links). Also rejected: numbers in the fact that aren't in the quote, time-sensitive
   facts (valuation, funding...) from undated sources, duplicates. `is_forum` comes from the domain, not the LLM.
   If a source's *title* is hedged ("may acquire", "reportedly", "considering"), its facts are marked hedged and
   made to keep a hedge ("Reportedly, ...").
5. **coverage**: if interview or role has fewer than 2 grounded facts, search once more for that section.
   (TL;DR is built from interview + role facts.)
6. **write**: the LLM gets at most 30 grounded facts (role/posting and interview first, spread across sources; at
   least 5 company facts are kept so section 1 isn't empty), and writes the brief citing them as `[F3]`.
   Citation variants (`【F3】`, `[F1, F2]`, `[F1-F3]`, `(F3)`, a bare `F3.`) are normalized before checking. Section 2 is grouped by
   interview stage; TL;DR has one bullet each for format, key topics and a standout insight. Company facts are capped
   (10 total, 3 per article) and related facts are merged so "the acquisition" always names what it is.
7. **finalize** (code, no LLM): drops sentences that cite no fact or contain a number not in their facts; drops TL;DR
   interview claims not backed by an interview fact (with none, TL;DR says "No interview reports found."); caps the
   company section at 5 sentences; splits any sentence that mixes facts from different interview stages (one sentence
   per fact; a TL;DR bullet is trimmed to its first fact); restores the hedge on hedged facts; drops "questions to ask"
   about interview logistics, rumors/unconfirmed news, or based on interview facts; rewrites unattributed forum facts to "Candidates report..."; always renders all six
   sections in order (placeholder if empty, e.g. when the writer's output was cut off); renumbers lists;
   turns `[F3]` into numbered source links and lists only cited sources.

Each step prints a one-line count to stderr, ending with e.g.
`[pipeline] sources=18 with_text=14 extracted=96 grounded=61 rejected=35 retry=yes (role)`.

### Extraction cache
Extraction results are cached in `.cache/extract/` (one JSON file per URL + prompt version + company/role/level),
so re-running the same company skips those LLM calls. `--no-cache` forces fresh extraction (and refreshes the cache).
Bumping `PROMPT_VERSION` in `src/facts.py` invalidates old entries.

### Debug file
Every run writes a report: per-step counts, a facts-per-source table (url, full/snippet, extracted, grounded by
section, rejected), every grounded fact (snippet-grounded ones tagged), each rejected fact (reason, quote, source), extraction errors,
each dropped sentence and each forum rewrite. It goes to `<output>.debug.md` with `-o`, otherwise
`last_run.debug.md` (override with `--debug-file`).

Only search/extract APIs are used — no scraping.
