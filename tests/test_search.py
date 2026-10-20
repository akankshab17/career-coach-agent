from datetime import date

import helpers
import pytest
import search
from brief import collect_sources
from helpers import res

TODAY = date(2026, 10, 19)


# ---- level filter ----

def test_internal_is_not_intern_and_levels_classified():
    assert search.classify_level(res(1, title="Software Engineer, Internal Systems")) == "experienced"
    assert search.classify_level(res(2, title="Software Engineer Intern, Summer 2027")) == "intern"
    assert search.classify_level(res(3, title="Software Engineer, New Grad")) == "new-grad"
    assert search.classify_level(res(4, url="https://x.com/jobs/university-graduate-swe")) == "new-grad"


def test_pick_posting_keeps_one_posting_at_requested_level():
    postings = [
        res(1, "Software Engineer, New Grad", "https://stripe.com/jobs/1"),
        res(2, "Software Engineer Intern", "https://stripe.com/jobs/2"),
        res(3, "Backend Engineer, Core Technology", "https://stripe.com/jobs/3"),
        res(4, "Software Engineer, Money Movement", "https://stripe.com/jobs/4"),
    ]
    assert [r["url"] for r in search.pick_posting(postings, "Stripe", "Software Engineer", "intern")] == ["https://stripe.com/jobs/2"]
    assert [r["url"] for r in search.pick_posting(postings, "Stripe", "Software Engineer", "new-grad")] == ["https://stripe.com/jobs/1"]
    picked = search.pick_posting(postings, "Stripe", "Software Engineer", "experienced")
    assert [r["url"] for r in picked] == ["https://stripe.com/jobs/4"]  # better title match of the two
    assert search.pick_posting(postings[:2], "Stripe", "Software Engineer", "experienced") == []


def test_posting_search_skipped_when_jd_given_and_reddit_queried(monkeypatch):
    queries = []
    monkeypatch.setattr(search, "search", lambda q, client=None, **kw: queries.append(q) or [])
    monkeypatch.setattr(search, "extract", helpers.fake_extract)

    search.gather_research("Acme", "SRE", "new-grad", jd="We need an SRE", today=TODAY)
    assert not any("job posting" in q for q in queries)
    assert any(q.startswith("site:reddit.com") for q in queries)

    queries.clear()
    search.gather_research("Acme", "SRE", "new-grad", jd=None, today=TODAY)
    assert any("job posting" in q and "new grad" in q for q in queries)


# ---- full page text ----

def test_fetch_gets_all_interview_and_forum_sources_but_only_top_company(monkeypatch):
    requested = []
    monkeypatch.setattr(search, "extract", lambda urls, client=None: requested.extend(urls) or {u: "text" for u in urls})
    research = {
        "company": [res(n) for n in range(1, 6)] + [res(6, url="https://www.reddit.com/r/acme/6")],
        "interview": [res(n) for n in range(7, 12)],
        "posting": [res(12, full_text="already have it")],
    }
    sources = collect_sources(research, "Acme")
    fetched, failed = search.fetch_full_text(sources)

    company_urls = [f"https://example.com/{n}" for n in (1, 2, 3)]  # top FULL_TEXT_COMPANY
    interview_urls = [f"https://example.com/{n}" for n in range(7, 12)]  # all of them
    assert requested == company_urls + ["https://www.reddit.com/r/acme/6"] + interview_urls
    assert (fetched, failed) == (9, 0)

    requested.clear()
    search.fetch_full_text(sources)  # second call (e.g. after a retry) doesn't refetch anything
    assert requested == []


def test_extract_batches_and_reports_failures(capsys):
    class Client:
        def __init__(self):
            self.batches = []

        def extract(self, urls):
            self.batches.append(len(urls))
            if len(self.batches) == 2:
                raise TimeoutError("slow")
            return {"results": [{"url": u, "raw_content": "page"} for u in urls[1:]], "failed_results": [{"url": urls[0]}]}

    client = Client()
    urls = [f"https://example.com/{n}" for n in range(25)]
    texts = search.extract(urls, client=client)
    assert client.batches == [20, 5]
    assert len(texts) == 19  # first batch minus its one failure; second batch timed out
    err = capsys.readouterr().err
    assert "could not extract https://example.com/0" in err and "extract failed" in err


# ---- recency + source ranking ----

def test_dates_parsed_stale_dropped_and_company_ranked_first():
    assert search._parse_date("Tue, 03 Feb 2026 12:00:00 GMT") == "2026-02-03"
    assert search._parse_date("2026-02-03T10:00:00Z") == "2026-02-03"
    assert search._parse_date(None) is None

    old, new, undated = res(1, published="2025-01-01"), res(2, published="2026-08-01"), res(3)
    assert search.drop_stale([old, new, undated], TODAY) == [new, undated]

    other = res(4, url="https://blog.example.com/x", published="2026-09-01")
    reuters = res(5, url="https://www.reuters.com/x", published="2026-01-01")
    own = res(6, url="https://stripe.com/newsroom", published="2025-12-01")
    assert search.rank([other, reuters, own], "Stripe") == [own, reuters, other]


# ---- job posting resolution ----

def test_stripe_apply_url_becomes_listing_url():
    assert search.normalize_posting_url("https://stripe.com/careers/apply/software-engineer-new-grad/8128744") == \
        "https://stripe.com/careers/listing/software-engineer-new-grad/8128744"
    other = "https://acme.com/careers/apply/1"
    assert search.normalize_posting_url(other) == other


def test_resolve_posting_tries_next_result_when_extract_fails_or_is_short(monkeypatch):
    tried = []

    def fake(urls, client=None):
        tried.extend(urls)
        return {urls[0]: "too short"} if urls[0].endswith("/job/1") else {urls[0]: "x" * 600}

    monkeypatch.setattr(search, "extract", fake)
    out = search.resolve_posting([res(1, url="https://stripe.com/careers/apply/job/1"),
                                  res(2, url="https://stripe.com/careers/apply/job/2")])
    assert len(out) == 1 and out[0]["full_text"] == "x" * 600
    assert out[0]["url"] == "https://stripe.com/careers/listing/job/2"  # the apply page is never cited
    assert tried == ["https://stripe.com/careers/listing/job/1", "https://stripe.com/careers/listing/job/2"]


def test_resolve_posting_falls_back_to_snippet_of_best_result(monkeypatch, capsys):
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {})
    candidates = [res(1, url="https://acme.com/jobs/1"), res(2, url="https://acme.com/jobs/2")]
    out = search.resolve_posting(candidates)
    assert out == [candidates[0]] and "full_text" not in out[0]
    assert "using the snippet" in capsys.readouterr().err
    assert search.resolve_posting([]) == []


# ---- follow-up search ----

def test_search_more_skips_known_urls_and_other_levels(monkeypatch):
    results = [
        res(1, "Software Engineer Intern", "https://acme.com/jobs/1"),
        res(2, "Software Engineer, New Grad", "https://acme.com/jobs/2"),
        res(3, "Software Engineer, New Grad 2027", "https://acme.com/jobs/3"),
    ]
    monkeypatch.setattr(search, "search", lambda q, client=None, **kw: results)
    out = search.search_more("Acme", "Software Engineer", "new-grad", "role", known_urls={"https://acme.com/jobs/2"})
    assert [r["url"] for r in out] == ["https://acme.com/jobs/3"]

    out = search.search_more("Acme", "Software Engineer", "new-grad", "interview", known_urls=set())
    assert len(out) == 3  # three queries returning the same results, deduped


# ---- page cleaning + relevance ----

LEETCODE_PAGE = (
    "[](https://leetcode.com/)\n*   [Problems](https://leetcode.com/problemset)\n*   [Contest](https://leetcode.com/contest)\n"
    + "[Register](https://leetcode.com/accounts/signup/?next=%2Fdiscuss%2Fpost%2F8372404%2Fstripe-backend)or Log in\n" * 70
    + "![](https://assets.leetcode.com/x.png)\n\nStripe - Backend Engineer Interview Experience\n\n1.   Online Assessment\n"
)


def test_clean_page_text_drops_link_markup_so_post_fits_in_the_cap():
    assert LEETCODE_PAGE.find("Stripe - Backend") > search.FULL_TEXT_CHARS
    cleaned = search.clean_page_text(LEETCODE_PAGE)
    assert "](" not in cleaned and "https://" not in cleaned
    assert "Problems" in cleaned  # link text kept
    assert cleaned.find("Stripe - Backend") < 1500


def test_extract_cleans_before_capping():
    class Client:
        def extract(self, urls):
            return {"results": [{"url": urls[0], "raw_content": LEETCODE_PAGE}]}

    text = search.extract(["https://leetcode.com/discuss/post/8372404"], client=Client())["https://leetcode.com/discuss/post/8372404"]
    assert "Stripe - Backend Engineer Interview Experience" in text


def test_interview_results_must_name_the_company_in_title_or_url():
    ok_title = res(1, "Stripe new grad interview", "https://www.reddit.com/r/cs/1")
    ok_url = res(2, "Onsite experience", "https://leetcode.com/discuss/post/1/stripe-onsite")
    hpe_mentions_it = res(3, "HPE intern interview", "https://www.reddit.com/r/csMajors/3",
                          content="Stripe asked X. Stripe's recruiter said Y. At Stripe the loop is Z.")  # about another company
    generic = res(4, "Internship Interview Questions: 17 With Sample Answers", "https://ophyai.com/blog/x")
    kept, filtered = search.filter_interview([ok_title, ok_url, hpe_mentions_it, generic], "Stripe", "Software Engineer")
    assert kept == [ok_title, ok_url]
    assert [(f["url"], f["reason"]) for f in filtered] == [(hpe_mentions_it["url"], "other-company"), (generic["url"], "other-company")]


def test_role_family_filter_keeps_google_swe_intern_and_drops_silicon_and_hpe():
    role = "Software Engineering Intern"
    swe = res(1, "Google SWE Intern 2025 Interview Experience", "https://leetcode.com/discuss/post/6034578/Google-SWE-Intern-2025-Interview-Experience")
    winter = res(2, "Google software engineering winter intern", "https://www.reddit.com/r/InterviewCoderHQ/comments/1tu8xk8/google_software_engineering_winter_intern")
    silicon = res(3, "Google Silicon Engineering Intern interview advice", "https://www.reddit.com/r/FPGA/comments/1bbac2t/google_silicon_engineering_intern_interview_advice",
                  content="They asked about Verilog, RTL and FPGA.")
    hpe = res(4, "HPE Software Engineering Intern GHC interview", "https://www.reddit.com/r/csMajors/comments/19ekjoy/hpe_software_engineering_intern_ghc_interview",
              content="Applied to Google too. Google rejected me. Google OA was hard.")
    swe_silicon = res(5, "Software Engineer, Silicon - Google interview", "https://example.com/swe-silicon")  # names both families
    generic_but_hardware = res(6, "Google intern interview", "https://www.reddit.com/r/csMajors/comments/1o0sfb8/google_intern_interview",
                               content="Verilog and RTL design, FPGA board bring-up, a bit of software.")
    generic_ok = res(7, "Google intern interview", "https://www.reddit.com/r/csMajors/comments/1/google_intern_interview",
                     content="Two coding rounds, software engineering questions about graphs. The recruiter was kind.")
    kept, filtered = search.filter_interview([swe, winter, silicon, hpe, swe_silicon, generic_but_hardware, generic_ok], "Google", role)
    assert [r["url"] for r in kept] == [swe["url"], winter["url"], swe_silicon["url"], generic_ok["url"]]
    reasons = {f["url"]: f["reason"] for f in filtered}
    assert reasons[silicon["url"]] == "other-role-family (wanted software)"
    assert reasons[generic_but_hardware["url"]] == "other-role-family (wanted software)"
    assert reasons[hpe["url"]] == "other-company"  # HPE post that only talks about Google in passing


def test_role_family_detected_from_the_role_text():
    assert search.role_family("Software Engineering Intern") == "software"
    assert search.role_family("Backend Engineer") == "software"
    assert search.role_family("Silicon Engineer") == "hardware"
    assert search.role_family("Data Scientist") == "data"
    assert search.role_family("Product Manager") == "product"
    assert search.role_family("Barista") is None  # unknown role: no family filtering


def test_search_more_reports_filtered_interview_results(monkeypatch):
    results = [res(1, "Stripe interview", "https://www.reddit.com/r/cs/1"), res(2, "Boeing careers", "https://jobs.boeing.com")]
    monkeypatch.setattr(search, "search", lambda q, client=None, **kw: results)
    filtered = []
    out = search.search_more("Stripe", "Software Engineer", "new-grad", "interview", known_urls=set(), filtered_out=filtered)
    assert [r["url"] for r in out] == ["https://www.reddit.com/r/cs/1"]
    assert [(f["url"], f["reason"]) for f in filtered] == [("https://jobs.boeing.com", "other-company")]


# ---- glassdoor is never touched ----

def test_glassdoor_hosts_are_blocked_everywhere():
    for url in ["https://www.glassdoor.com/Interview/Google-x.htm", "https://www.glassdoor.co.in/Interview/Stripe.htm",
                "https://glassdoor.ca/x", "https://de.glassdoor.com/x"]:
        assert search.is_blocked(url), url
    assert not search.is_blocked("https://www.reddit.com/r/glassdoor_stories/1")
    assert not search.is_blocked("https://example.com/glassdoor")


def test_search_excludes_glassdoor_in_the_query_and_in_results():
    class Client:
        def search(self, query, max_results, **options):
            self.options = options
            return {"results": [
                {"title": "G", "url": "https://www.glassdoor.com/Interview/Google.htm", "content": "x"},
                {"title": "R", "url": "https://www.reddit.com/r/cs/1", "content": "y"},
            ]}

    client = Client()
    out = search.search("site:reddit.com Google interview", client=client, exclude_domains=["youtube.com"])
    assert [r["url"] for r in out] == ["https://www.reddit.com/r/cs/1"]
    assert {"glassdoor.com", "youtube.com"} <= set(client.options["exclude_domains"])


def test_extract_never_sends_glassdoor_urls(capsys):
    class Client:
        def __init__(self):
            self.sent = []

        def extract(self, urls):
            self.sent += urls
            return {"results": [{"url": u, "raw_content": "page text"} for u in urls]}

    client = Client()
    texts = search.extract(["https://www.glassdoor.com/Interview/x.htm", "https://example.com/1"], client=client)
    assert client.sent == ["https://example.com/1"] and list(texts) == ["https://example.com/1"]
    assert "refusing to extract 1 blocked page" in capsys.readouterr().err
    assert search.extract(["https://www.glassdoor.co.in/x"], client=client) == {}  # nothing left to send


def test_blocked_urls_never_become_sources():
    sources = collect_sources({"interview": [res(1, url="https://www.glassdoor.com/Interview/x.htm"), res(2)], "filtered": [{"url": "u"}]}, "Acme")
    assert [s["url"] for s in sources.values()] == ["https://example.com/2"]


# ---- source types ----

@pytest.mark.parametrize("url, group, published, expected", [
    ("https://stripe.com/careers/listing/software-engineer-new-grad/8128744", "posting", None, "official-posting"),
    ("https://boards.greenhouse.io/stripe/jobs/123", "posting", None, "official-posting"),
    ("https://jobs.lever.co/stripe/abc", "role", None, "official-posting"),
    ("https://www.stripe.com/jobs/search?q=swe", "role", None, "official-posting"),
    ("https://www.stripe.com/company/careers/university/new-grads", "careers", None, "company-site"),
    ("https://stripe.com/newsroom/news/q4-results", "company", "2026-02-01", "company-site"),
    ("https://jobright.ai/jobs/early-career-apm", "posting", None, "job-aggregator"),
    ("https://www.themuse.com/jobs/stripe/swe", "posting", None, "job-aggregator"),
    ("https://www.linkedin.com/jobs/view/1", "role", None, "job-aggregator"),
    ("https://www.reddit.com/r/cs/1", "interview", None, "forum/blog"),
    ("https://medium.com/@x/my-stripe-interview", "interview", None, "forum/blog"),
    ("https://leetcode.com/discuss/post/1/stripe", "interview", None, "forum/blog"),
    ("https://www.reuters.com/tech/stripe-1", "company", "2026-02-01", "news"),
    ("https://some-outlet.example.com/stripe-deal", "company", "2026-02-01", "news"),
    ("https://www.joinleland.com/library/a/stripe-apm", "interview", None, "third-party-guide"),
    ("https://igotanoffer.com/blogs/tech/stripe-interview", "interview", None, "third-party-guide"),
])
def test_source_types(url, group, published, expected):
    assert search.source_type(url, "Stripe", group, published) == expected


def test_careers_pages_are_recognised_on_the_company_site_only():
    assert search.is_careers_page("https://www.salesforce.com/company/careers/university/new-grads", "Salesforce")
    assert search.is_careers_page("https://www.salesforce.com/anything", "Salesforce", group="careers")
    assert not search.is_careers_page("https://www.salesforce.com/news/stories/fy26-q4-highlights", "Salesforce")
    assert not search.is_careers_page("https://www.joinleland.com/careers/salesforce", "Salesforce")


# ---- choosing the useful part of a page ----

MENU = "".join(f"## Product {n}\n\nShort blurb about product {n}.\n\n" for n in range(300))  # ~12k chars of site menu
CONTENT = (
    "# New graduate jobs at Acme.\n\nLooking to launch your career at Acme? You are in the right place.\n\n"
    "## Associate Product Manager Program\n\nThis two-year rotational program for new graduates gives you ownership of "
    "products, mentorship from senior leaders, and exposure to the APM community. Eligibility: a degree by 2027.\n\n"
    "## What you will do\n\nYou will own a defined area of a product, run customer discovery interviews, partner with engineers "
    "and designers, and present your product strategy to senior leaders every quarter during the program.\n\n"
    "## Ready to apply?\n\nApplications open in early July. Submit your application promptly."
)
KEYWORDS = search.focus_keywords("Acme", "Associate Product Manager", "new-grad")


def test_focus_text_finds_the_content_after_a_long_menu():
    page = MENU + CONTENT
    assert page.find("New graduate jobs") > search.FULL_TEXT_CHARS  # the old head-cap would have kept only the menu
    focused = search.focus_text(page, KEYWORDS)
    assert len(focused) <= search.FULL_TEXT_CHARS
    assert focused.startswith("# New graduate jobs at Acme.") and focused.endswith("Submit your application promptly.")
    assert "Product 7" not in focused


def test_focus_text_keeps_content_that_comes_first_and_short_pages_whole():
    page = CONTENT + "\n\n" + MENU
    focused = search.focus_text(page, KEYWORDS)
    assert focused.startswith("# New graduate jobs at Acme.") and "Applications open in early July" in focused
    assert search.focus_text(CONTENT, KEYWORDS) == CONTENT  # fits: untouched
    assert search.focus_text(MENU + CONTENT, None) == (MENU + CONTENT)[:search.FULL_TEXT_CHARS]  # no keywords: the head


def test_focus_keywords_use_the_role_level_and_generic_words_but_not_the_company():
    kw = search.focus_keywords("Acme", "Associate Product Manager", "new-grad")
    assert {"associate", "product", "manager", "new grad", "graduate", "apply", "interview"} <= set(kw)
    assert "acme" not in kw
    assert "intern" in search.focus_keywords("Acme", "SWE", "intern")


def test_extract_returns_the_whole_cleaned_page_and_fetch_focuses_it(monkeypatch):
    class Client:
        def extract(self, urls):
            return {"results": [{"url": urls[0], "raw_content": MENU + CONTENT}]}

    text = search.extract(["https://acme.com/careers/new-grads"], client=Client())["https://acme.com/careers/new-grads"]
    assert len(text) > search.FULL_TEXT_CHARS and "Applications open in early July" in text

    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: text for u in urls})
    sources = collect_sources({"careers": [res(1, url="https://acme.com/careers/new-grads")]}, "Acme")
    search.fetch_full_text(sources, focus=KEYWORDS)
    got = sources["S1"]["full_text"]
    assert len(got) <= search.FULL_TEXT_CHARS and got.startswith("# New graduate jobs at Acme.")

    sources = collect_sources({"careers": [res(1, url="https://acme.com/careers/new-grads")]}, "Acme")
    search.fetch_full_text(sources)  # without a focus: the head, as before
    assert "New graduate jobs" not in sources["S1"]["full_text"]


def test_resolve_posting_focuses_long_pages_too(monkeypatch):
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: MENU + CONTENT for u in urls})
    out = search.resolve_posting([res(1, url="https://acme.com/careers/apm")], "United States", KEYWORDS)
    assert out[0]["full_text"].startswith("# New graduate jobs at Acme.")


# ---- interview queries fit the role family ----

@pytest.mark.parametrize("role, expected, absent", [
    ("Associate Product Manager", ["product sense", "APM interview", "case interview"], ["SQL"]),
    ("Data Scientist", ["SQL", "case study"], ["product sense"]),
    ("Software Engineer", ["coding"], ["product sense", "SQL"]),
    ("Barista", [], ["product sense", "SQL", "coding"]),
])
def test_interview_queries_use_terms_for_the_role_family(monkeypatch, role, expected, absent):
    queries = []
    monkeypatch.setattr(search, "search", lambda q, client=None, **kw: queries.append(q) or [])
    search.gather_research("Acme", role, "new-grad", "a jd")
    interview_queries = [q for q in queries if "interview" in q]
    assert interview_queries
    for term in expected:
        assert all(term in q for q in interview_queries), (term, interview_queries)
    for term in absent:
        assert not any(term in q for q in interview_queries)

    queries.clear()
    search.search_more("Acme", role, "new-grad", "interview", set())
    for term in expected:
        assert any(term in q for q in queries)


def test_company_careers_pages_are_searched_and_kept_only_if_on_the_company_site(monkeypatch):
    seen = {}

    def fake_search(q, client=None, **kw):
        seen[q] = True
        if "careers" in q and "program" in q:
            return [res(1, "New grads", "https://www.acme.com/company/careers/university/new-grads"),
                    res(2, "Best APM programs", "https://www.aakashg.com/apm-programs"),
                    res(3, "Acme APM", "https://www.reddit.com/r/apm/1")]
        return []

    monkeypatch.setattr(search, "search", fake_search)
    research = search.gather_research("Acme", "Associate Product Manager", "new-grad", "a jd")
    assert [r["url"] for r in research["careers"]] == ["https://www.acme.com/company/careers/university/new-grads"]
    assert list(research)[0] == "careers"  # first, so a page found twice is filed under careers, not company news
