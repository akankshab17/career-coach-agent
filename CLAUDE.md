# CLAUDE.md

## What this is
A career-coach research agent. Input: company + role + level (`intern | new-grad | experienced`, default new-grad) +
location (default "United States") + optional pasted JD. Output: a markdown interview-prep brief where every sentence
cites a source, built only from facts that were checked against the source text. CLI: `python src/main.py --company X --role Y`.
The user is learning: build in small steps, explain design decisions, stop after each step so they can test.

## Pipeline (LangGraph, `src/graph.py`), in order
`search -> fetch -> extract -> ground -> coverage -> [search_more -> fetch -> extract -> ground -> coverage, once] -> write -> finalize`
- **search**: Tavily queries (company overview + news, the company's own careers pages (open web + `include_domains` on the
  company's domain), interview experiences (short `site:reddit.com` / `site:leetcode.com/discuss` queries, <= 6 words after
  `site:`, listing pages without `/comments/` or a post id dropped; plus one general query), the posting). **fetch**: full page text via Tavily extract. **extract**: one LLM call per source (parallel, `EXTRACT_CONCURRENCY` default 4), told the page's purpose
  (company/role/interview), returns facts as JSON `{fact, exact_quote, section, stage, level, about_company}`. **ground**: code keeps a fact only if its quote is in the page.
  **coverage**: fewer than 2 grounded interview or role facts triggers ONE follow-up search. **write**: one LLM call, grounded
  facts only. **finalize**: code-only checks, citations, rendering. Each step prints a one-line count to stderr. Tavily credits: each search/fetch/search_more line ends with `; tavily credits: N` (reported via `include_usage`, else estimated: search 1, extract 1 per 5 pages); `[pipeline]` has the brief total. The eval harness ignores `include_usage` in fixture keys.

## Files
- `src/main.py` CLI (`--level --location --jd -o --no-cache --debug-file`); writes the brief and a `.debug.md` run report.
- `src/graph.py` state + nodes + wiring; stops early (`PipelineError`) if every extraction fails.
- `src/search.py` Tavily search/extract, source kinds and types, interview relevance filters (company in title/URL + role family),
  job-posting ranking (level, location, rejects job-list pages), page cleaning, `focus_text` page selection, Glassdoor block.
- `src/location.py` text heuristics: which country a posting is in, the posting's location line.
- `src/facts.py` extraction prompt (`PROMPT_VERSION`), on-disk cache, `ground()`, hedges, wrong-subject check, location fact.
- `src/guidance.py` general prep/questions per role family, used only when a brief has no interview or role facts.
- `src/ranking.py` fact usefulness for the writer: specificity, near-duplicate merging, product/tech news first.
- `src/brief.py` source ids/types, `RunStats` + debug report, writer prompt, `select_for_writing`, `check_body` (all finalize rules),
  citation normalizer, sentence splitter, section enforcement, rendering.
- `src/llm.py` the ONLY file that knows providers (groq default, openai): model routing, retries, rate-limit classification, usage log,
  OpenAI fallback + spend tracking (`.cache/spend.json`).
- `src/api.py` FastAPI web API (`uvicorn --app-dir src --factory api:serve`): POST/GET /briefs, ONE worker thread (llm.py globals are
  per-run, so never run two pipelines at once), progress via `RunStats.on_progress` (graph wraps every node), reuse < 7 days,
  optional `APP_ACCESS_CODE` (X-Access-Code header). `src/jobs.py` SQLite store `.cache/jobs.db` (the table is the queue;
  `running` rows are re-queued at startup). `app.py` Streamlit client (talks only to the API, `BRIEF_API_URL`).
- Deploy (Hugging Face Spaces, Docker SDK): `Dockerfile` + `start.sh` (API on 127.0.0.1:8000, Streamlit on $PORT=7860, the only
  exposed port), `.dockerignore` (no .env/.cache/evals/tests in the image), README YAML header = Space config. Image env:
  `REQUIRE_ACCESS_CODE=1` (serve() refuses to start without an APP_ACCESS_CODE of 16+ chars), `PRIVATE_BRIEFS=1` (GET /briefs
  403; the app lists only this browser session's briefs), `MAX_BRIEFS_PER_DAY=5` (Tavily free plan: 1,000 credits/month; new briefs per UTC day, all users; reuse
  is free; 429 with the reset time). Free Space disk is ephemeral: jobs.db, extraction cache and spend.json reset on
  restart; the user's prepaid OpenAI balance (auto-reload off) is the real fallback cap. `requirements.txt` = pinned
  runtime deps; `requirements-dev.txt` adds pytest. The Dockerfile creates /home/user/app/.cache and chowns it to uid 1000
  before `USER` (WORKDIR alone makes it root-owned); `RUN_DOCKER_TESTS=1` runs the real image test.
- `evals/` eval harness (below). `tests/` pytest; `tests/helpers.py` has the fakes (`fake_llm`, `res`, `fact`).
  `tests/test_api.py` (TestClient + `fake_runner`), `tests/test_app.py` (Streamlit AppTest routed into the TestClient).

## Key design decisions (and why)
- **Extract-then-write.** The first design (write, then LLM-verify) dropped ~58% of sentences: the writer embellished and the
  verifier deleted. Now facts are extracted per source first; the writer sees only grounded facts and cites them as `[F3]`.
- **Grounding is code, not an LLM.** A fact survives only if its `exact_quote` appears in the source text (normalised for case,
  whitespace, punctuation, dashes, markdown links). Also rejected: numbers in the fact that aren't in the quote, time-sensitive
  facts (valuation, funding, `$X billion`, headcount) from undated sources, duplicates, wrong-subject facts (about the publishing
  site). Rejections are logged with reasons in the debug file. There is deliberately no second LLM verifier.
- **Finalize is code-only**: drops uncited sentences and numbers not in the cited facts (years matching a source date are OK),
  rewrites forum interview facts to "Candidates report", keeps hedges ("Reportedly"), labels other-level interview/role facts ("(full-time process)", "(internship role)"),
  splits sentences that mix interview stages, keeps timelines out of TL;DR/prep/questions, blocks company news in prep/questions,
  always renders all six sections (placeholder if empty), opens TL;DR with "Limited interview data (N sources)" if < 3 interview facts.
  With no interview/role facts at all, empty sections 4-5 get code-written general guidance for the role family (`src/guidance.py`),
  labelled "General guidance (no company-specific data found)": the only uncited text allowed; eval metric `general_sections`.
- **Model routing.** `EXTRACT_MODEL` (default groq `openai/gpt-oss-20b`: many small calls) vs `WRITE_MODEL` (`openai/gpt-oss-120b`: one call).
  `LLM_MODEL` sets both. **Reasoning effort**: `EXTRACT_REASONING_EFFORT` default low, `WRITE_REASONING_EFFORT` default medium; sent
  to Groq gpt-oss and OpenAI reasoning models (gpt-5*, o*) (hidden reasoning is billed as output tokens). Daily limits (TPD/RPD/quota)
  raise `QuotaExceededError`; per-minute limits retry using the provider's wait (cap 30s).
- **Fallback.** A Groq `QuotaExceededError` switches THAT task (Groq limits are per model) to `FALLBACK_PROVIDER` /
  `FALLBACK_EXTRACT_MODEL` / `FALLBACK_WRITE_MODEL` for the rest of the run (`llm.reset_fallback()` at the start of main and of an eval
  run). Every call logs `[llm] <task> via <provider>/<model>`. OpenAI spend accumulates in `.cache/spend.json`; at `OPENAI_BUDGET_USD`
  (default 3.00) the fallback raises `BudgetExceededError` (a `QuotaExceededError`, so runs/evals stop). No temperature/seed for OpenAI
  reasoning models (they reject it). Tests redirect `SPEND_PATH` (conftest): never write the real spend file.
- **Page purpose in the extraction prompt.** Told only the role, gpt-oss-20b at low effort returned `[]` for company profiles and
  careers pages that never name the role (Cummins: all 5 pages; Google: Forbes). The prompt now says what each page is for.
- **Writer sampling**: temperature 0 + seed (`SAMPLING` in `llm.py`; best effort on Groq). Extraction stays at 0.2.
- **Parallel extraction.** `graph.extract_parallel` runs a bounded thread pool; results are assembled in source order, so the
  same input gives the same candidates/log (only the `in Xs, N at a time` suffix of the [extract] line varies). A per-source
  error is logged for that source; a `FatalLLMError` (daily limit without fallback, budget, bad key) cancels unstarted
  sources and stops the run. `llm.py` guards `_calls`, `_fallback`, `_truncated` and the spend file with `_lock`; parallel
  daily-limit hits switch once and all retry on the fallback. The cache key uses `llm.served_by()` (per thread), never
  `get_settings()` after a call (another thread may have switched meanwhile). Budget can overshoot by up to N-1 calls.
- **Extraction cache** (`.cache/extract/`) key = prompt version + extraction model + URL + company/role/level + text kind + page purpose +
  hash of the exact text read. Facts from a fallback call are filed under the model that actually answered. NOT the reasoning effort. Bump `PROMPT_VERSION` only when the extraction prompt changes; a model change or different
  page text already gets a new key. Don't cache by URL alone: an empty result from reading a site menu once stuck forever.
- **Source types**: `official-posting | company-site | third-party-guide | job-aggregator | forum/blog | news`. Official = JD, company posting,
  or careers page. No official text -> section 3 says so and labels each fact by source type. Official sources' facts are sent first.
- **Location/level**: postings in `--location` are preferred, another country's posting is never used if one in the location exists;
  a posting whose text REQUIRES another country ("must be located in the UK") is excluded (`location.required_location_elsewhere`);
  postings at other levels ignored ("Engineer I"/"Associate"/"Entry Level" = new-grad). A posting must belong to the company (title,
  URL, or an employer line in the snippet): Siemens once stood in for Cummins. Company fetch slots go to pages whose title names the company. Interview facts carry the level their source describes; matching levels preferred, others labelled.
- **Page focus by purpose**: pages <= 6000 chars go whole. Longer ones: keep the 6000 most relevant chars, scored with keywords for the
  page's purpose (company pages: company words; careers/postings: role words; interview pages: process words). Career pages open with
  thousands of chars of menu, so taking the head gave the extractor only the menu. Reddit usually can't be fetched: the search
  snippet is used (grounded against the snippet, tagged `[snippet-grounded]`).
- **Writer gets <= 30 facts**: interview + role first (spread across sources; the candidate's level first), >= 5 company facts, <= 4
  timeline facts, pinned location fact, and >= 3 interview slots for forum/blog reports when any exist (official pages rank
  first and once took every slot). Ranking (`src/ranking.py`): specificity score (named problems/topics/questions, numbers,
  requirements up; marketing/meta down); role facts by source tier (official-posting/JD, company-site, guide, aggregator...);
  near-duplicates merged into one fact with `also_sources` (rendered as several links; different numbers never merge);
  product/tech company news first for engineering families. Personal dates from forum/blog are rejected (`personal-date`);
  the location fact only comes from `is_official_posting` sources.
- **Levels on role facts too** (extract-v6): an intern program's duties or a manager posting's requirements are other-level role
  facts; a posting's title/URL level (`search.explicit_level`) overrides the extractor. **Dedupe by `canonical_url`** (language
  segments ignored, English kept). **FAQ/nav quotes** (a question on the company's own site) are rejected (`faq-or-navigation`).

## Eval harness (`evals/`)
- Cases in `evals/cases.yaml` (Google intern, Cummins new-grad SWE, Perplexity AI Engineer, Salesforce APM, Amazon new-grad SDE). Record/replay happens at the
  Tavily client, so all our logic runs in replay; only the network is replaced; the recording date is pinned.
- `python evals/run_evals.py --record` (live; the USER runs this: it spends Tavily credits and Groq tokens) -> `evals/fixtures/<case>/`.
  Default = replay (no Tavily; LLM still runs, extraction cache helps). `--from-facts` = writer + finalize only from `facts.json`
  (refreshed by every record/full replay). Other flags: `--cases a,b --no-cache --no-fail --set-baseline --list`, `--repeat N`
  (writer + finalize N times per case from the same facts; writer metrics averaged). Each case records the provider/model per task
  (`llm`); a provider change vs the compared run is a `≠ CHANGED` warning, not a regression.
- Prints a table per case, saves `evals/results/<timestamp>.json`, compares with the previous run and `evals/baseline.json`, flags worse
  metrics (writer metrics only if worse by > 2), exit 1 on regression, 2 if aborted (daily limit).
- **Fixtures and results are gitignored** (third-party page text; repo is public). `evals/baseline.json` (metrics only) IS committed.

## Conventions
- Run `.venv/bin/pytest -q` yourself after changes and report the real result. Never deploy: the user pushes to the Space.
  Keep `requirements.txt` pinned to versions the tests passed with. Python 3.12 venv (LangGraph needs 3.10+).
- Never scrape Glassdoor (blocked in search, extract and sources). Search APIs only.
- Never commit `.env`, eval fixtures, `eval_*.md`, `*.debug.md`. No API keys in fixtures (the harness refuses).
- For big changes: explain the design first and WAIT for the user's OK. Otherwise do what was asked, then stop so they can test.
- Don't redo finished work: check the code/tests first. Update README + tests with each behaviour change.
- Web app: keep `app.py` a thin API client; pipeline changes go in `src/`. Never start a second pipeline thread in the API.
- Use `git` only as asked; commit messages end with the Co-Authored-By line from the session's attribution reminder.

## Known limitations
- Exact-quote grounding is strict: slightly altered quotes cause false rejections. The debug file lists every rejection to check.
- Finalize can't catch a meaning shift in plain words (it checks citations, numbers, formats, not semantics).
- Location, role-family, level and source-type detection are hand-written heuristics (lexicons, regexes); extend `location.py` /
  `search.py` tables when a case misclassifies. The interview relevance filter needs the company in the title/URL.
- Page focus is heuristic; unusual layouts can still hide content. Reddit/Blind pages are snippet-only.
- Extraction is LLM-sampled: uncached runs vary a little. Groq's free tier has daily token limits (gpt-oss-20b/120b are separate).
- Cached extraction is stale if a page changes only in ways the text hash can't see; use `--no-cache` to refresh.
- Evals need a human-run `--record` first; replay misses (changed queries) must be re-recorded.
