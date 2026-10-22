---
title: Interview Brief
sdk: docker
app_port: 7860
pinned: false
---

# Career Coach Research Agent

Searches the web (Tavily) and writes a cited interview-prep brief (Groq or OpenAI, switchable).

## Setup
```
python3.12 -m venv .venv && source .venv/bin/activate   # Python 3.10+ (LangGraph)
pip install -r requirements-dev.txt   # runtime deps (requirements.txt, pinned) + pytest
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
Reasoning effort is sent as `reasoning_effort` to Groq's gpt-oss models and OpenAI's reasoning models (gpt-5*, o1/o3/o4...);
other models ignore it. gpt-oss
"thinks" before answering and that hidden reasoning is billed as output tokens, so `low` makes the many small extraction
calls much cheaper and faster. It is not part of the extraction cache key (cached facts are reused whatever the effort).

The writer runs at temperature 0 with a fixed seed (`SAMPLING` in `src/llm.py`) so the same facts give nearly the same draft;
Groq and OpenAI only promise best-effort determinism, so wording can still vary a little. Extraction stays at 0.2.
OpenAI reasoning models reject a non-default temperature, so they get no temperature or seed.

### Fallback when Groq's daily limit runs out
```
FALLBACK_PROVIDER=openai
FALLBACK_EXTRACT_MODEL=gpt-5.6-luna
FALLBACK_WRITE_MODEL=gpt-5.4-mini
OPENAI_BUDGET_USD=3.00      # default 3.00
```
Only a Groq **daily** limit (`QuotaExceededError`) triggers it; per-minute limits still just wait and retry. The switch is per task
(Groq's limits are per model: gpt-oss-20b and gpt-oss-120b run out separately) and lasts for the rest of the run (an eval run
counts as one run); the next run starts on Groq again. The stderr log names the provider/model of every call
(`[llm] extract via openai/gpt-5.6-luna [FALLBACK] effort=low in=... out=... $0.0012 (OpenAI total $0.41 of $3.00)`) and a
`FALLBACK:` line marks the switch. Facts extracted by the fallback model are cached under that model, never under Groq's.
Every OpenAI call's cost (tokens x `PRICES`) is added to `.cache/spend.json`, a running total across runs (delete it to start
from zero). Once it reaches `OPENAI_BUDGET_USD` the fallback refuses to make more calls and the run stops with
`BudgetExceededError` (evals abort, like a daily limit). A fallback model with no entry in `PRICES` is refused, since its spend
couldn't be checked.

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

## Web app
A FastAPI backend (`src/api.py`) runs the pipeline in the background; a Streamlit front end (`app.py`) talks to it.
```
uvicorn --app-dir src --factory api:serve --host 127.0.0.1 --port 8000     # terminal 1: the API (reads .env)
streamlit run app.py                                                       # terminal 2: the app, at http://localhost:8501
```
- **API:** `POST /briefs {company, role, level?, location?, jd?, force?}` returns `{job_id, status, reused}` at once (202).
  `GET /briefs/{job_id}`: `status` (queued / running / done / failed), the current `step`, the step log lines, and when done
  `brief_markdown` + `debug_summary` (counts, retry, models/providers, tokens, cost); when failed, `error`.
  `GET /briefs?limit=20` lists recent briefs, `GET /briefs/{job_id}/debug` returns the full debug report, `GET /health` is open.
- **One job at a time:** a single background worker takes jobs oldest first (llm.py keeps per-run usage and fallback state in
  module globals, and the Groq limits are shared anyway). Each job is its own run: usage and the fallback switch reset per job.
  At most 20 jobs can wait (429 after that).
- **Jobs live in SQLite** (`.cache/jobs.db`, gitignored). After a restart, queued jobs and jobs that were running when the
  server stopped run again from the start (the extraction cache makes the repeat cheap).
- **Reuse:** the same company/role/location (case and spaces ignored), level and JD within 7 days returns the existing job
  id with `"reused": true` instead of running again; so does an identical job still in progress. `force: true` (the app's
  "Run again" box) always runs. Failed jobs are never reused.
- **The app:** a form (company, role, level, location, optional JD), a live progress bar (search -> fetch -> extract -> ground ->
  write -> finalize, polled every second), the brief with clickable sources, a download button (`.md`), run details, and
  recent briefs in the sidebar (each one opens via `?job=<id>`). `BRIEF_API_URL` points it at the API (default
  `http://localhost:8000`).
- **Access code (`APP_ACCESS_CODE` in `.env`):** if set, every API endpoint except `/health` needs it in the `X-Access-Code`
  header (401 otherwise) and the app asks for it once per browser session. If unset there is no check, which is only fine
  on your own machine. Before deploying publicly: set it, serve both over HTTPS (the code travels in a header), and keep the
  API reachable only where the app can call it. There is no lockout after wrong codes, so use a long random code.

## Deploy to Hugging Face Spaces (free)
The block at the top of this README (`sdk: docker`, `app_port: 7860`) is the Space's configuration; GitHub shows it as a
small table. The `Dockerfile` runs both processes in one container (`start.sh`): the API on `127.0.0.1:8000`, reachable only
from inside the container, and Streamlit on port 7860, the only port the Space exposes (over HTTPS). Production settings
baked into the image: `REQUIRE_ACCESS_CODE=1` (the API refuses to start without an `APP_ACCESS_CODE` of 16+ characters),
`PRIVATE_BRIEFS=1` (no shared list of briefs; each browser session sees only its own, since JDs can hold student details),
`MAX_BRIEFS_PER_DAY=5` (new briefs per UTC day for everyone together; reusing a recent brief is free; `0` = no limit; sized
for Tavily's free plan of 1,000 credits a month, see "Tavily credits" below) and
`EXTRACT_CONCURRENCY=2`. Any of them can be overridden with a Space variable. Keys exist only as Space secrets.

**What the free Space does not keep.** The disk is wiped whenever the Space restarts, sleeps (after 48 hours without
visitors) or is rebuilt: the jobs database (briefs, the 7-day reuse, today's count toward the daily limit), the extraction
cache and `.cache/spend.json`. Because `spend.json` starts at $0 after every restart, `OPENAI_BUDGET_USD` cannot cap OpenAI
spending across restarts. **The real cap is a prepaid OpenAI balance with auto-reload turned off**: when it runs out,
fallback calls fail and the brief fails with a clear message. Keep it that way while the fallback is on.

**Tavily credits.** Every step logs what it cost: `[search] ...; tavily credits: 9`, `[fetch] ...; tavily credits: 2`,
`[search_more] ...; tavily credits: 4`, and the `[pipeline]` line has the brief's total (`tavily_credits=11`); the web app's
run details and the eval table show it too. The numbers are what Tavily reports for each call; for an old recorded fixture
without usage data they are estimated from the price list (basic search 1 credit, extract 1 credit per 5 pages). Measured on
the five eval cases: about 9-12 credits for search + fetch, plus roughly 3-6 when the follow-up search runs. So the free plan's
1,000 credits a month cover about 70-100 briefs (2-3 a day). `MAX_BRIEFS_PER_DAY=5`, fully used every day, would need about
1,650-2,500 credits a month: it stops a single busy day from draining the plan, not the whole month. Set it to 2 or 3 to
stay inside the free plan even at full use.

### 1. Test the image on your machine
```
docker build -t interview-brief .

# It must refuse to start without an access code (expect "[start] the API exited during startup" and exit code 1):
docker run --rm interview-brief; echo "exit code: $?"

# Run it with your keys from .env and a fresh code:
set -a; source .env; set +a
export APP_ACCESS_CODE="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"; echo "code: $APP_ACCESS_CODE"
docker run --rm --name interview-brief -p 7860:7860 \
  -e TAVILY_API_KEY -e GROQ_API_KEY -e OPENAI_API_KEY \
  -e FALLBACK_PROVIDER -e FALLBACK_EXTRACT_MODEL -e FALLBACK_WRITE_MODEL -e OPENAI_BUDGET_USD \
  -e APP_ACCESS_CODE \
  interview-brief
```
In a second terminal:
```
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7860/      # 200: Streamlit is up
curl -s --max-time 3 http://localhost:8000/health || echo "API not reachable from outside: good"
docker exec interview-brief python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
```
Then open http://localhost:7860: it asks for the code; enter it and build one brief (this one makes real Tavily/LLM
calls). Stop with Ctrl+C.

### 2. Create the Space
1. On huggingface.co: **New Space**. Name it (e.g. `interview-brief`), **SDK: Docker** (blank template), **Hardware: CPU
   basic (free)**, **Public** (coaches don't need an account; the access code protects it), then **Create Space**.
2. In the Space: **Settings -> Variables and secrets**.
   - **New secret** for each: `TAVILY_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `APP_ACCESS_CODE` (generate a new one with
     `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`; don't reuse the local test code).
   - **New variable** for each: `FALLBACK_PROVIDER=openai`, `FALLBACK_EXTRACT_MODEL=gpt-5.6-luna`,
     `FALLBACK_WRITE_MODEL=gpt-5.4-mini`, `OPENAI_BUDGET_USD=3.00`. Optional: `MAX_BRIEFS_PER_DAY`, `EXTRACT_CONCURRENCY`.
3. Create a token at huggingface.co/settings/tokens with **write** access (git uses it as the password).

### 3. Push the code
```
git log --all --oneline -- .env      # must print nothing: .env was never committed
git remote add space https://huggingface.co/spaces/<HF_USERNAME>/interview-brief
git push --force space main          # username: your HF username, password: the write token
```
`--force` replaces the starter README the Space was created with (only the first time). Later updates: commit, then
`git push space main` (and `git push origin main` for GitHub).

### 4. Check it
- **Logs** tab: the build finishes, then the container log shows `Uvicorn running on http://127.0.0.1:8000` and
  `You can now view your Streamlit app`. If it shows `REQUIRE_ACCESS_CODE is on but APP_ACCESS_CODE is not set`, the
  secret is missing or misspelled.
- Open `https://<HF_USERNAME>-interview-brief.hf.space`: it asks for the code; build one brief.
- `curl -s https://<HF_USERNAME>-interview-brief.hf.space/health | head -c 200` returns Streamlit's HTML, not the API's JSON:
  the API is not exposed.
- Send coaches the URL and the code separately. To change the code, edit the secret; the Space restarts (and forgets its
  briefs).

## Test
```
pytest
```
`tests/test_api.py` covers the API (fake pipeline, temporary database); `tests/test_app.py` drives `app.py` with Streamlit's
`AppTest` against that API. Neither makes network calls.

`--level` is `intern`, `new-grad` or `experienced`; the default is `new-grad`. Postings at other levels are ignored
(a title with "Engineer I", "Associate" or "Entry Level" counts as new-grad). A posting must be the company's own: the company
is in its title or URL, or the search snippet has an employer line ("Company: Cummins").
`--location` (default `United States`) picks where the job should be: postings in it are preferred, and a posting from
another country is never used when one in the location exists. Section 3 opens with the posting's location (read from the
page by code; marked "outside <location>" if it's elsewhere). A posting whose text requires you to be somewhere else
("requires that you are located in the UK") is rejected, even if the page also lists offices in your location; it is listed
under "Excluded postings" in the debug file.

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
   Careers pages are also searched on the company's own domain only (Tavily `include_domains`, the domain taken from company
   pages the overview/news search found, else `<name>.com`), with a short query: the open-web careers query can return
   nothing from the company at all (one Salesforce recording: Verizon, USAJOBS and job lists), losing the program page.
   Every source is tagged with a type: `official-posting`, `company-site`, `third-party-guide`, `job-aggregator`,
   `forum/blog` or `news`.
2. **fetch**: full page text (Tavily extract) for the posting, every interview/forum source, and the top 3 company sources
   (pages whose title names the company first, so a story that mentions it in passing doesn't take a slot).
   Link/image markup is stripped. A page that fits in 6,000 chars is passed whole. For a longer one, the most relevant
   6,000 chars are kept (not the first 6,000: career pages open with thousands of chars of site menu). Relevance depends on
   what the page is *for*: careers pages and postings are scored with role/level/application words, news and about pages
   with company words (the company's name, founded, headquarters, products, acquired, customers, CEO...), interview guides
   and forum posts with process words (interview, round, onsite, assessment...). Menu entries and headings score nothing,
   and a page with no clear signal keeps its head.
   Pages that can't be fetched (Reddit, usually) fall back to their search snippet, which is still real source text.
   A posting must describe one job: GitHub repos of postings, "Jobs in New York" / "Job Board" / "Open Positions" pages and
   results pages full of "Apply now / Posted 2 days ago" lines are rejected (by title/URL, or by their text once fetched).
   glassdoor.* is blocked everywhere (never searched, extracted or cited).
   The same page in two languages (`amazon.jobs/content/en/...` and `/content/pt/...`) is one source: URLs are compared
   without scheme, `www.` and language segments, and the English variant is kept.
   Forum queries are short (at most 6 words after `site:`): `site:reddit.com <Company> <role> interview` and
   `site:leetcode.com/discuss <Company> <level> interview` (the follow-up search asks for "interview experience" and LeetCode
   by role). A 16-word Reddit query returned random subreddit home pages. Forum listing pages (a subreddit's home page, i.e. a
   Reddit URL without `/comments/`, or a LeetCode discuss index without a post id) are dropped before filtering, and the
   `[search]` line says how many forum results survived: `forum results: 3 of 10 kept (5 listing pages)`.
   Interview results are filtered for relevance: the company must be in the title or URL, and the role family
   (software, hardware, data, product...) must match, so an HPE post or a "Silicon/FPGA intern" post can't stand in for
   "Google Software Engineering Intern". What was filtered is logged and listed in the debug file. Stripe `/careers/apply/` URLs become `/careers/listing/`; if a posting page
   can't be extracted (or is under 500 chars) the next-best posting at your level is tried.
3. **extract**: one LLM call per source (full page or snippet), up to `EXTRACT_CONCURRENCY` (default 4, max 16) at once.
   Facts are assembled in source order whatever order the calls finish in, so the same input gives the same output (evals
   stay comparable); the `[extract]` line ends with the wall time, e.g. `... in 14.2s, 4 at a time`. Each call backs off on a
   per-minute limit as before; a daily limit switches every worker to the fallback (one `FALLBACK:` line), or stops the whole
   run if none is configured; with calls in parallel the OpenAI budget can be overshot by up to `EXTRACT_CONCURRENCY - 1`
   calls. Each source's call returns facts as
   `{fact, exact_quote, section, stage, level, about_company}`. `section` is `company`, `interview`, `role` or `timeline`
   (application dates and deadlines, which are not interview content).
   `stage` (interview facts): `oa`, `phone-screen`, `team-screen`, `onsite` or `unknown`.
   `level` (interview and role facts): the seniority the process, role or posting is for: `intern`, `new-grad`, `experienced`
   or `unknown`. For a role fact from a job posting, a level stated in the posting's title/URL wins ("Software Development
   Manager", "Senior", "Engineer II" = experienced). Role and interview facts for another level are sent to the writer only
   after the candidate's own level, and are labelled in the brief ("(internship role)", "(experienced role)", "(full-time process)").
   FAQ questions, menu items and headings are not facts: the prompt says so, and a quote that is a question on the company's
   own site ("Can I apply to multiple roles?") is rejected as `faq-or-navigation`. Questions quoted in forum posts and guides
   are kept (they are often real interview questions).
   `about_company`: false for facts about the publishing site (e.g. an interview-prep site's own practice tool) or another company.
   The extractor is told each page's purpose (`company`, `role` or `interview`, the same purpose page focus uses) and to extract
   what that page is good for even if it never mentions the role: told only the role, it returned nothing for company profiles
   and for careers pages that don't name the role.
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
   least 5 company facts are kept so section 1 isn't empty), and writes the brief citing them as `[F3]`. Which facts
   (`select_for_writing` + `src/ranking.py`, all code):
   - **Specific beats generic.** Named problems and topics ("a variation of LRU Cache and a Top K problem", "Course Schedule
     II"), spelled-out questions, numbers and concrete requirements score up; marketing ("self-motivated", "powerful
     technologies", "talent community") and "the page includes..." score down. Interview facts go specific band first.
   - **Role facts: the official posting (or your JD) first**, then the company's own site, guides, aggregators; most specific
     first within each; marketing last whatever its source.
   - **Near-duplicates merge**: the same claim from several sources (>= 60% word overlap, same section, same numbers) becomes
     one fact that cites every source.
   - **Company news for engineering roles**: product/tech news before stock and earnings news.
   - Grounding drops **personal dates** from forum/blog posts ("took the OA on January 6, 2025": `personal-date`); durations
     ("offer after six business days") stay. The **posting location** comes only from an official posting or your JD, never an
     aggregator; Indeed search pages (`/q-...-jobs.html`) are job lists, not postings.
   - A date label the writer puts after the full stop ("... ecosystem. (reported Dec 2025) [F12]") is moved back inside the
     sentence, which was otherwise dropped as uncited.
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
   turns `[F3]` into numbered source links and lists only cited sources, each as a `[Title](url)` link.
   **Only company facts?** If no interview or role fact reached the writer, an empty "How to prepare" / "Questions to ask"
   gets general guidance for the role family (software, product, data, ...; written in `src/guidance.py`, not by the LLM),
   labelled **General guidance (no company-specific data found)**, and the TL;DR says so. These are the only uncited lines a
   brief can contain. Sections 2 and 3 keep their placeholders: there is no general version of a company's process.

Each step prints a one-line count to stderr, ending with e.g.
`[pipeline] sources=18 with_text=14 extracted=96 grounded=61 rejected=35 retry=yes (role)`.

### Extraction cache
Extraction results are cached in `.cache/extract/` (one JSON file per URL + prompt version + extraction model + company/role/level
+ page purpose + a hash of the exact text the extractor read, so a change in how pages are cleaned or focused never reuses an old, e.g. empty, result),
so re-running the same company skips those LLM calls. `--no-cache` forces fresh extraction (and refreshes the cache).
Switching `EXTRACT_MODEL` gives that model its own entries (never another model's facts). Bump `PROMPT_VERSION` in
`src/facts.py` only when the extraction prompt itself changes.

### Debug file
Every run writes a report: per-step counts, a facts-per-source table (url, full/snippet, extracted, grounded by
section, rejected), every grounded fact (snippet-grounded ones tagged), each rejected fact (reason, quote, source), extraction errors,
each dropped sentence and each forum rewrite. It goes to `<output>.debug.md` with `-o`, otherwise
`last_run.debug.md` (override with `--debug-file`).

Only search/extract APIs are used — no scraping.

## Evals (record / replay)
Five fixed cases (`evals/cases.yaml`: Google intern, Cummins new-grad SWE, Perplexity AI Engineer, Salesforce APM, Amazon new-grad SDE) are run
through the whole pipeline and scored, so a change can be checked for regressions instead of by eye.

```
python evals/run_evals.py --record        # run live once; saves search results + page texts + facts to evals/fixtures/<case>/
python evals/run_evals.py                 # replay: no Tavily calls (needs the fixtures); LLM calls still happen (extraction cache helps)
python evals/run_evals.py --from-facts    # replay only writer + finalize from each case's saved facts.json (test writer changes cheaply)
```
Options: `--cases a,b` (subset), `--no-cache` (redo extraction), `--no-fail` (exit 0 even on regressions),
`--set-baseline` (pin this run's metrics as `evals/baseline.json`), `--list`, `--repeat N` (see below).

- **`--repeat N`:** the writer is the noisiest step (on identical facts Salesforce once dropped 8 sentences, then 0). With
  `--repeat N` each case runs once as usual, then writer + finalize run N-1 more times from that run's `facts.json`; sentences
  dropped and rewrites become the mean of the N writer runs (each run's value is shown and saved as `writer_runs`; extra briefs are
  saved as `<case>.repeatK.md`). Extraction, token and latency numbers come from the first run only.

- **How record/replay works:** at the Tavily client, the only network seam. Everything of ours still runs in replay (queries,
  filters, page cleaning and focusing, extraction, grounding, writing, finalize). The recording date is pinned, so the 12-month
  recency filter doesn't drift. Anything the code asks for that wasn't recorded (e.g. after you change a search query) is printed as
  `[replay] MISSING ...`, counted as `fixture misses`, and flagged: re-record that case.
- **Fixtures are not committed** (`evals/fixtures/` is gitignored: they hold third-party page text). Neither is `evals/results/`.
  `evals/baseline.json` (metrics only) is committed. Fixtures refuse to be written if they contain an API key.
- **Table per case:** sources, full-text pages, facts by section, grounded, rejected, sentences dropped, rewrites, empty sections,
  fixture misses, latency, tokens. Results go to `evals/results/<timestamp>.json` (plus each brief, debug file and log).
- **Provider per case:** each case records which provider/model served each task (`llm` in the results JSON and the table). If a
  task's provider differs from the run it is compared with (e.g. the fallback kicked in), the table marks it `≠ CHANGED` and a
  WARNING lists it: those metrics compare different models. It's a warning, not a regression (the exit code doesn't change).
- **Comparison** with the previous run (same kind) and with the baseline; `▼ WORSE` marks a metric that got worse. Exit code 1 if
  any did, 2 if the run aborted (e.g. a daily LLM limit; aborted runs are never used for comparisons).
  Writer metrics (sentences dropped, rewrites) are flagged only if worse by more than 2. Extraction/grounding metrics are strict
  when both runs had the same extraction-cache hit rate, otherwise they get a tolerance (a fresh LLM sample differs a little).
  Latency is judged only when the cache hit rates match; tokens, within 25%.
- `--from-facts` reads `facts.json`, which every `--record` and full replay refreshes, so run a full replay after changing
  extraction or grounding. Its extraction numbers are carried over, not recomputed, so only writer metrics are compared and it can't
  set the baseline.

