# Career Coach Research Agent

Searches the web (Tavily) and writes a cited interview-prep brief (Groq or OpenAI, switchable).

## Setup
```
python3.12 -m venv .venv && source .venv/bin/activate   # Python 3.10+ (LangGraph)
pip install -r requirements.txt
cp .env.example .env   # then fill in TAVILY_API_KEY and GROQ_API_KEY
```

## Choose an LLM provider and models
All optional, set in `.env`:
```
LLM_PROVIDER=groq      # or: openai
EXTRACT_MODEL=         # many small fact-extraction calls. Default: groq openai/gpt-oss-20b, openai gpt-4o-mini
WRITE_MODEL=           # the one writing call.            Default: groq openai/gpt-oss-120b, openai gpt-4o-mini
LLM_MODEL=             # optional single model for both tasks (EXTRACT_MODEL / WRITE_MODEL win when set)
```
```
EXTRACT_REASONING_EFFORT=   # low | medium | high; blank = low
WRITE_REASONING_EFFORT=     # low | medium | high; blank = medium
```
Reasoning effort is sent to Groq as `reasoning_effort` for gpt-oss models only; other providers and models ignore it. gpt-oss
"thinks" before answering and that hidden reasoning is billed as output tokens, so `low` makes the many small extraction
calls much cheaper and faster. It is not part of the extraction cache key (cached facts are reused whatever the effort).

OpenAI needs `OPENAI_API_KEY`; Groq needs `GROQ_API_KEY`. To see which models your key can use right now (e.g. if a default gets retired):
```
python src/llm.py models          # uses LLM_PROVIDER
python src/llm.py models openai
```
Each run prints a one-line summary to stderr: provider, calls, tokens, latency, estimated cost, and calls/tokens
**per model**, e.g. `out=9300 tok (of which reasoning=4100) ... | by model: openai/gpt-oss-20b 24 calls in=40100 out=7000 (reasoning=2900, effort=low); openai/gpt-oss-120b 1 calls in=9100 out=2300 (reasoning=1200, effort=medium)`.
Reasoning tokens are already counted inside the output tokens; they are shown separately only when the provider returns them.
Prices live in `PRICES` in `src/llm.py`; a model with no entry shows cost as n/a.

### Rate limits and stopping early
- A per-minute limit (TPM) is retried twice, waiting what the provider asks (capped at 30s), then a clear error.
- A **daily** limit (tokens/requests per day, or an exhausted account quota) is never retried. The run stops at once with the
  wait time the provider gave, e.g. `try again in 8m 35s (around 14:32 local time)`. A bad or missing API key also stops the run.
  Extractions already done are cached, so re-running continues where it stopped.
- If **every** extraction fails (and there are no earlier facts), the run stops before any further search or writing.
- When a run stops early, the debug file still gets the steps logged so far plus the reason, and the exit code is non-zero.

## Run
```
python src/main.py --company "Stripe" --role "Backend Engineer" --level new-grad --location "United States"
python src/main.py --company "Stripe" --role "Backend Engineer" --jd "$(cat jd.txt)" -o brief.md
```

## Test
```
pytest
```

`--level` is `intern`, `new-grad` or `experienced`; the default is `new-grad`. Postings at other levels are ignored.
`--location` (default `United States`) picks where the job should be: postings in it are preferred, and a posting from
another country is never used when one in the location exists. Section 3 opens with the posting's location (read from the
page by code; marked "outside <location>" if it's elsewhere).

## How it works: extract, then write
The pipeline is a LangGraph graph (`src/graph.py`):

```
search -> fetch -> extract -> ground -> coverage --enough--> write -> finalize
            ^                              |
            +------ search_more <--too few-+   (at most once)
```

1. **search**: Tavily search for company news, the company's own careers/program pages, interview experiences (incl. Reddit)
   and the posting at your level. Interview queries add words that fit the role family (product: "product sense", "APM
   interview", "case interview"; data: "SQL", "case study"; software: "coding", "system design"; ...), not just coding terms.
   Every source is tagged with a type: `official-posting`, `company-site`, `third-party-guide`, `job-aggregator`,
   `forum/blog` or `news`.
2. **fetch**: full page text (Tavily extract) for the posting, every interview/forum source, and the top 3 company sources.
   Link/image markup is stripped, then the most relevant 6,000 chars of a long page are kept (not the first 6,000): career
   pages open with thousands of chars of site menu, and the real content comes after it. Blocks of prose are scored by
   role/level/process keywords; menu entries and headings score nothing.
   Pages that can't be fetched (Reddit, usually) fall back to their search snippet, which is still real source text.
   glassdoor.* is blocked everywhere (never searched, extracted or cited).
   Interview results are filtered for relevance: the company must be in the title or URL, and the role family
   (software, hardware, data, product...) must match, so an HPE post or a "Silicon/FPGA intern" post can't stand in for
   "Google Software Engineering Intern". What was filtered is logged and listed in the debug file. Stripe `/careers/apply/` URLs become `/careers/listing/`; if a posting page
   can't be extracted (or is under 500 chars) the next-best posting at your level is tried.
3. **extract**: one LLM call per source (full page or snippet) returns facts as
   `{fact, exact_quote, section, stage, level, about_company}`. `section` is `company`, `interview`, `role` or `timeline`
   (application dates and deadlines, which are not interview content).
   `stage` (interview facts): `oa`, `phone-screen`, `team-screen`, `onsite` or `unknown`.
   `level` (interview facts): the seniority the source describes: `intern`, `new-grad`, `experienced` or `unknown`.
   `about_company`: false for facts about the publishing site (e.g. an interview-prep site's own practice tool) or another company.
4. **ground** (code, no LLM): a fact is kept only if its `exact_quote` appears in the page text or snippet (ignoring
   case, whitespace, punctuation, curly quotes, dashes, ellipses and markdown links). Also rejected: numbers in the fact that aren't in the quote, time-sensitive
   facts from undated sources, duplicates, and wrong-subject facts (flagged by the extractor, or naming the publishing
   site's brand but not the company). Time-sensitive covers valuation/funding/revenue, bare "$X billion/million" amounts and
   headcounts ("8,000 employees", "~67,400-employee"). `is_experience` comes from the domain and section, not the LLM: only
   *interview* facts from first-person sources (Reddit, LeetCode discuss, Medium/blogs) are "Candidates report" material,
   so a job repost on Reddit is not.
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
   interview claims not backed by an interview fact; with fewer than 3 interview facts the TL;DR opens with
   "Limited interview data (N sources)." (and "No interview reports found." when there are none); labels interview facts from
   another level, e.g. "(full-time process)"; drops "How to prepare" / "Questions" items that cite company news or timeline facts; keeps timelines out of the TL;DR
   (they live under a "Timeline" sub-heading in section 2); ignores a year in a sentence when a cited source is dated that
   year (or the "(reported Mon YYYY)" label gives it); when section 3 has no official text (your JD, the company's posting,
   or its careers page), opens it with "No official posting text found; based on <source type>." and labels each fact with
   its source type; caps the
   company section at 5 sentences; splits any sentence that mixes facts from different interview stages (one sentence
   per fact; a TL;DR bullet is trimmed to its first fact); restores the hedge on hedged facts; drops "questions to ask"
   about interview logistics, rumors/unconfirmed news, or based on interview facts; rewrites unattributed forum facts to "Candidates report..."; always renders all six
   sections in order (placeholder if empty, e.g. when the writer's output was cut off); renumbers lists;
   turns `[F3]` into numbered source links and lists only cited sources.

Each step prints a one-line count to stderr, ending with e.g.
`[pipeline] sources=18 with_text=14 extracted=96 grounded=61 rejected=35 retry=yes (role)`.

### Extraction cache
Extraction results are cached in `.cache/extract/` (one JSON file per URL + prompt version + extraction model + company/role/level
+ a hash of the exact text the extractor read, so a change in how pages are cleaned or focused never reuses an old, e.g. empty, result),
so re-running the same company skips those LLM calls. `--no-cache` forces fresh extraction (and refreshes the cache).
Switching `EXTRACT_MODEL` gives that model its own entries (never another model's facts). Bump `PROMPT_VERSION` in
`src/facts.py` only when the extraction prompt itself changes.

### Debug file
Every run writes a report: per-step counts, a facts-per-source table (url, full/snippet, extracted, grounded by
section, rejected), every grounded fact (snippet-grounded ones tagged), each rejected fact (reason, quote, source), extraction errors,
each dropped sentence and each forum rewrite. It goes to `<output>.debug.md` with `-o`, otherwise
`last_run.debug.md` (override with `--debug-file`).

Only search/extract APIs are used — no scraping.
