# CLAUDE.md

## What this is
A career-coach research agent. Input: company + role + level (`intern | new-grad | experienced`, default new-grad) +
location (default "United States") + optional pasted JD. Output: a markdown interview-prep brief where every sentence
cites a source, built only from facts that were checked against the source text. CLI: `python src/main.py --company X --role Y`.
The user is learning: build in small steps, explain design decisions, stop after each step so they can test.

## Pipeline (LangGraph, `src/graph.py`), in order
`search -> fetch -> extract -> ground -> coverage -> [search_more -> fetch -> extract -> ground -> coverage, once] -> write -> finalize`
- **search**: Tavily queries (company overview + news, the company's own careers pages, interview experiences incl. Reddit,
  the posting). **fetch**: full page text via Tavily extract. **extract**: one LLM call per source, told the page's purpose
  (company/role/interview), returns facts as JSON `{fact, exact_quote, section, stage, level, about_company}`. **ground**: code keeps a fact only if its quote is in the page.
  **coverage**: fewer than 2 grounded interview or role facts triggers ONE follow-up search. **write**: one LLM call, grounded
  facts only. **finalize**: code-only checks, citations, rendering. Each step prints a one-line count to stderr.

## Files
- `src/main.py` CLI (`--level --location --jd -o --no-cache --debug-file`); writes the brief and a `.debug.md` run report.
- `src/graph.py` state + nodes + wiring; stops early (`PipelineError`) if every extraction fails.
- `src/search.py` Tavily search/extract, source kinds and types, interview relevance filters (company in title/URL + role family),
  job-posting ranking (level, location, rejects job-list pages), page cleaning, `focus_text` page selection, Glassdoor block.
- `src/location.py` text heuristics: which country a posting is in, the posting's location line.
- `src/facts.py` extraction prompt (`PROMPT_VERSION`), on-disk cache, `ground()`, hedges, wrong-subject check, location fact.
- `src/brief.py` source ids/types, `RunStats` + debug report, writer prompt, `select_for_writing`, `check_body` (all finalize rules),
  citation normalizer, sentence splitter, section enforcement, rendering.
- `src/llm.py` the ONLY file that knows providers (groq default, openai): model routing, retries, rate-limit classification, usage log.
- `evals/` eval harness (below). `tests/` pytest; `tests/helpers.py` has the fakes (`fake_llm`, `res`, `fact`).

## Key design decisions (and why)
- **Extract-then-write.** The first design (write, then LLM-verify) dropped ~58% of sentences: the writer embellished and the
  verifier deleted. Now facts are extracted per source first; the writer sees only grounded facts and cites them as `[F3]`.
- **Grounding is code, not an LLM.** A fact survives only if its `exact_quote` appears in the source text (normalised for case,
  whitespace, punctuation, dashes, markdown links). Also rejected: numbers in the fact that aren't in the quote, time-sensitive
  facts (valuation, funding, `$X billion`, headcount) from undated sources, duplicates, wrong-subject facts (about the publishing
  site). Rejections are logged with reasons in the debug file. There is deliberately no second LLM verifier.
- **Finalize is code-only**: drops uncited sentences and numbers not in the cited facts (years matching a source date are OK),
  rewrites forum interview facts to "Candidates report", keeps hedges ("Reportedly"), labels other-level facts "(full-time process)",
  splits sentences that mix interview stages, keeps timelines out of TL;DR/prep/questions, blocks company news in prep/questions,
  always renders all six sections (placeholder if empty), opens TL;DR with "Limited interview data (N sources)" if < 3 interview facts.
- **Model routing.** `EXTRACT_MODEL` (default groq `openai/gpt-oss-20b`: many small calls) vs `WRITE_MODEL` (`openai/gpt-oss-120b`: one call).
  `LLM_MODEL` sets both. **Reasoning effort**: `EXTRACT_REASONING_EFFORT` default low, `WRITE_REASONING_EFFORT` default medium; sent only
  to Groq gpt-oss models (hidden reasoning is billed as output tokens). Daily limits (TPD/RPD/quota) raise `QuotaExceededError` and stop
  the run with the wait time; per-minute limits retry using the provider's wait (cap 30s).
- **Page purpose in the extraction prompt.** Told only the role, gpt-oss-20b at low effort returned `[]` for company profiles and
  careers pages that never name the role (Cummins: all 5 pages; Google: Forbes). The prompt now says what each page is for.
- **Writer sampling**: temperature 0 + seed (`SAMPLING` in `llm.py`; best effort on Groq). Extraction stays at 0.2.
- **Extraction cache** (`.cache/extract/`) key = prompt version + extraction model + URL + company/role/level + text kind + page purpose +
  hash of the exact text read. NOT the reasoning effort. Bump `PROMPT_VERSION` only when the extraction prompt changes; a model change or different
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
- **Writer gets <= 30 facts**: interview + role first (spread across sources), >= 5 company facts, <= 4 timeline facts, pinned location fact.

## Eval harness (`evals/`)
- Cases in `evals/cases.yaml` (Google intern, Cummins new-grad SWE, Perplexity AI Engineer, Salesforce APM). Record/replay happens at the
  Tavily client, so all our logic runs in replay; only the network is replaced; the recording date is pinned.
- `python evals/run_evals.py --record` (live; the USER runs this: it spends Tavily credits and Groq tokens) -> `evals/fixtures/<case>/`.
  Default = replay (no Tavily; LLM still runs, extraction cache helps). `--from-facts` = writer + finalize only from `facts.json`
  (refreshed by every record/full replay). Other flags: `--cases a,b --no-cache --no-fail --set-baseline --list`, `--repeat N`
  (writer + finalize N times per case from the same facts; writer metrics averaged).
- Prints a table per case, saves `evals/results/<timestamp>.json`, compares with the previous run and `evals/baseline.json`, flags worse
  metrics (writer metrics only if worse by > 2), exit 1 on regression, 2 if aborted (daily limit).
- **Fixtures and results are gitignored** (third-party page text; repo is public). `evals/baseline.json` (metrics only) IS committed.

## Conventions
- Run `.venv/bin/pytest -q` yourself after changes and report the real result. Python 3.12 venv (LangGraph needs 3.10+).
- Never scrape Glassdoor (blocked in search, extract and sources). Search APIs only.
- Never commit `.env`, eval fixtures, `eval_*.md`, `*.debug.md`. No API keys in fixtures (the harness refuses).
- For big changes: explain the design first and WAIT for the user's OK. Otherwise do what was asked, then stop so they can test.
- Don't redo finished work: check the code/tests first. Update README + tests with each behaviour change.
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
