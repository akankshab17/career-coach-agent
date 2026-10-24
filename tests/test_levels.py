"""Level checks: postings and interview sources for another seniority than the candidate's (the Uber new-grad brief)."""
import pytest

import facts
import search
from helpers import res

PHD_II = "Graduate 2026 PhD Software Engineer II (AV Labs)"
LONG = "x" * 600  # posting text must be at least MIN_POSTING_CHARS


# ---- postings ----

@pytest.mark.parametrize("title", [
    PHD_II, "Senior Software Engineer, New Grad Hiring Team", "Staff Engineer - University Programs",
    "Principal Engineer (Graduate)", "Software Engineer III - Early Career", "Graduate Ph.D. Research Scientist",
])
def test_a_new_grad_request_rejects_postings_whose_title_signals_another_level(title):
    posting = res(1, title, "https://www.uber.com/global/en/careers/list/1/")
    assert search.posting_level_conflict(posting, "new-grad")
    assert search.rank_postings([posting], "Uber", "Software Engineer", "new-grad") == []


def test_the_phd_engineer_ii_posting_is_labelled_experienced_not_new_grad():
    assert search.classify_level(res(1, PHD_II)) == "new-grad"  # "Graduate" alone would have passed
    assert search.explicit_level(res(1, PHD_II)) == "experienced"


@pytest.mark.parametrize("snippet,years", [
    ("Minimum qualifications: 5+ years of software engineering experience.", 5),
    ("You have at least 6 years of professional experience building systems.", 6),
    ("3-5 yrs experience in backend development", 3),
])
def test_a_snippet_asking_for_years_of_experience_is_another_level(snippet, years):
    posting = res(1, "Software Engineer", "https://www.uber.com/careers/list/2/", content=snippet)
    assert search.posting_level_conflict(posting, "new-grad") == f"asks for {years}+ years of experience"
    assert search.posting_level_conflict(posting, "intern")
    assert search.posting_level_conflict(posting, "experienced") is None  # an experienced candidate wants exactly this


def test_real_new_grad_postings_pass():
    for posting in (res(1, "Software Engineer I - New Grad 2026", "https://uber.com/careers/list/3/",
                        content="BS, MS or PhD in Computer Science. 0-1 years of experience."),
                    res(2, "Software Engineer, University Graduate", "https://uber.com/careers/list/4/",
                        content="Bachelor's degree; 1+ years of experience through internships is a plus.")):
        assert search.posting_level_conflict(posting, "new-grad") is None  # a PhD in the body is fine; so is 0-1 years
        assert search.rank_postings([posting], "Uber", "Software Engineer", "new-grad")


def test_resolve_posting_skips_a_page_whose_text_asks_for_senior_experience(monkeypatch, capsys):
    pages = {"https://uber.com/careers/list/1/": LONG + " Requirements: 5+ years of experience in distributed systems.",
             "https://uber.com/careers/list/2/": LONG + " New grads welcome."}
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: pages[u] for u in urls})
    cands = [res(1, "Software Engineer New Grad", "https://uber.com/careers/list/1/"),
             res(2, "Software Engineer New Grad", "https://uber.com/careers/list/2/")]
    out = search.resolve_posting(cands, None, level="new-grad")
    assert [p["url"] for p in out] == ["https://uber.com/careers/list/2/"]
    assert "is not a new-grad posting (asks for 5+ years of experience)" in capsys.readouterr().err


def test_no_valid_posting_left_means_no_posting_not_the_rejected_snippet(monkeypatch, capsys):
    page = LONG + " You bring 6+ years of experience."
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: page for u in urls})
    out = search.resolve_posting([res(1, "Software Engineer New Grad", "https://uber.com/careers/list/1/")], None, level="new-grad")
    assert out == []  # section 3 then gets the general guidance
    assert "no new-grad posting found" in capsys.readouterr().err


def test_a_follow_up_posting_is_excluded_at_fetch_time_too(monkeypatch):
    url = "https://www.uber.com/global/en/careers/list/141436/"
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: LONG + " 5+ years of experience required." for u in urls})
    sources = {"S1": {**res(1, "Software Engineer", url), "group": "role", "kind": "company"}}
    search.fetch_full_text(sources, level="new-grad")
    assert "full_text" not in sources["S1"] and sources["S1"]["excluded"] == "not a new-grad posting: asks for 5+ years of experience"


def test_a_careers_page_mentioning_other_programs_years_is_not_excluded(monkeypatch):
    url = "https://uber.com/us/en/careers/university/"
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: LONG + " Experienced hires: 5+ years of experience." for u in urls})
    sources = {"S1": {**res(1, "Uber University", url), "group": "role", "kind": "company"}}
    search.fetch_full_text(sources, level="new-grad")
    assert "full_text" in sources["S1"] and "excluded" not in sources["S1"]


# ---- interview sources ----

@pytest.mark.parametrize("title", [
    "Uber L4 interview experience", "Uber L5A SWE onsite", "Uber SSE interview", "Uber Senior Software Engineer loop",
    "Uber SDE2 interview questions", "Uber SWE II phone screen", "Uber Staff Engineer interview",
])
def test_interview_pages_titled_for_senior_levels_are_dropped_for_a_new_grad(title):
    page = res(1, title, "https://leetcode.com/discuss/interview-experience/123456/x")
    kept, filtered = search.filter_interview([page], "Uber", "Software Engineer", "new-grad")
    assert kept == [] and filtered[0]["reason"].startswith("other-level")
    kept, _ = search.filter_interview([page], "Uber", "Software Engineer", "experienced")
    assert kept == [page]  # an experienced candidate keeps them


@pytest.mark.parametrize("content", [
    "I have 4 YOE at a fintech and interviewed for backend.", "YOE: 6, current company FAANG.",
    "Background: 5 years of experience in Java. Got the offer for L5A.",
])
def test_unlabelled_forum_posts_that_give_years_of_experience_are_dropped(content):
    post = res(1, "Uber interview experience", "https://www.reddit.com/r/cscareerquestions/comments/abc/uber_interview/", content=content)
    kept, filtered = search.filter_interview([post], "Uber", "Software Engineer", "new-grad")
    assert kept == [] and filtered[0]["reason"].startswith("other-level")


def test_new_grad_posts_and_guides_are_kept():
    new_grad = res(1, "Uber interview experience", "https://www.reddit.com/r/cscareerquestions/comments/abc/uber/",
                   content="New grad here, 0 YOE. Two coding rounds then a hiring manager chat.")
    guide = res(2, "Uber Software Engineer interview guide", "https://prep.example.com/uber-swe",
                content="Our coaches have 10+ years of experience at Uber.")  # a guide's marketing, not the candidate
    kept, _ = search.filter_interview([new_grad, guide], "Uber", "Software Engineer", "new-grad")
    assert kept == [new_grad, guide]


def test_a_forum_post_whose_text_is_senior_turns_its_facts_experienced_whatever_the_extractor_said():
    text = "Uber interview: I have 5 years of experience. The onsite had a system design round on rides matching."
    src = {"S1": {"url": "https://www.reddit.com/r/x/comments/1/uber/", "title": "Uber interview", "full_text": text, "kind": "forum"}}
    cand = {"fact": "The onsite had a system design round.", "exact_quote": "The onsite had a system design round on rides matching",
            "section": "interview", "level": "new-grad", "source_id": "S1"}
    grounded, _ = facts.ground([cand], src, company="Uber")
    assert grounded[0]["level"] == "experienced"  # then down-ranked and labelled "(full-time process)" by the writer step


def test_a_guide_title_fills_only_an_unknown_level():
    src = {"S1": {"url": "https://prep.example.com/uber-new-grad", "title": "Uber New Grad interview guide",
                  "full_text": "Expect two coding rounds and a behavioral round.", "kind": "other"}}
    cand = {"fact": "Expect two coding rounds.", "exact_quote": "Expect two coding rounds and a behavioral round",
            "section": "interview", "level": "unknown", "source_id": "S1"}
    assert facts.ground([cand], src, company="Uber")[0][0]["level"] == "new-grad"
    assert facts.ground([{**cand, "level": "intern"}], src, company="Uber")[0][0]["level"] == "intern"  # extractor kept


def test_company_pages_keep_the_extractors_level():
    src = {"S1": {"url": "https://uber.com/careers/university", "title": "Uber Senior and University hiring",
                  "full_text": "New grads have two coding interviews.", "kind": "company"}}
    cand = {"fact": "New grads have two coding interviews.", "exact_quote": "New grads have two coding interviews",
            "section": "interview", "level": "new-grad", "source_id": "S1"}
    assert facts.ground([cand], src, company="Uber")[0][0]["level"] == "new-grad"


# ---- company name ----

@pytest.mark.parametrize("typed,shown", [("uber", "Uber"), ("  jp   morgan ", "Jp Morgan"), ("eBay", "eBay"), ("IBM", "IBM")])
def test_a_lowercase_company_is_title_cased(typed, shown):
    assert search.display_company(typed) == shown


def test_level_codes_count_only_without_a_new_grad_marker_and_technical_staff_is_not_staff():
    amazon = res(1, "My Amazon Loop Interview Experience(L4 - New Grad)", "https://leetcode.com/discuss/interview-question/6135581/x")
    assert search.title_level(amazon) == "new-grad"  # Amazon's new-grad SDE I is L4
    assert search.title_level(res(2, "Uber L4 onsite", "https://leetcode.com/discuss/post/1234/x")) == "experienced"
    mts = res(3, "Member of Technical Staff (New Grad) at Perplexity", "https://startup.jobs/member-of-technical-staff-new-grad-1")
    assert search.title_level(mts) == "new-grad" and search.posting_level_conflict(mts, "new-grad") is None
    kept, _ = search.filter_interview([amazon, mts], "Amazon", "Software Developer", "new-grad")
    assert kept == [amazon]  # (mts names another company)


# ---- mid-level postings without a level in the title (Amazon "Software Development Engineer, Data Center Builder Tools") ----

AMAZON_MID = ("BASIC QUALIFICATIONS\n- 3 years of professional software development experience (non-internship)\n"
              "- 2 years of design or architecture experience\n")


@pytest.mark.parametrize("text", [
    AMAZON_MID,
    "- 3+ years of non-internship professional software development experience",
    "- 3 years of non-internship professional software development experience",
    "You have at least 3 years of experience shipping production code.",
    "Requirements: 3-5 years in backend development.",
    "Minimum of 4 years' experience with distributed systems.",
])
def test_any_phrasing_of_three_or_more_years_rejects_a_new_grad_posting(text):
    posting = res(1, "Software Development Engineer, Data Center Builder Tools", "https://www.amazon.jobs/en/jobs/10513782/x")
    assert search.posting_level_conflict(posting, "new-grad", LONG + text).startswith("asks for ")
    assert search.posting_level_conflict(posting, "intern", text)


@pytest.mark.parametrize("duty", [
    "Mentor junior engineers and review their designs.", "You will lead code reviews for the team.",
    "Drive technical design decisions across the org.", "Lead design decisions for new services.",
    "Own the architecture of our build tooling.",
])
def test_senior_duties_reject_a_new_grad_posting(duty):
    posting = res(1, "Software Development Engineer", "https://www.amazon.jobs/en/jobs/10513782/x")
    assert search.posting_level_conflict(posting, "new-grad", LONG + " " + duty).startswith("senior duties")
    assert search.posting_level_conflict(posting, "experienced", duty) is None


def test_a_real_new_grad_posting_still_passes():
    text = (LONG + " Bachelor's degree, 0-1 years of experience. You'll be mentored by senior engineers, take part in code "
            "reviews and contribute to design discussions. Amazon has 25 years of experience serving customers.")
    posting = res(1, "Software Development Engineer I, New Grad", "https://www.amazon.jobs/en/jobs/2000000/x")
    assert search.posting_level_conflict(posting, "new-grad", text) is None


def test_the_amazon_mid_level_posting_is_excluded_when_fetched(monkeypatch):
    url = "https://www.amazon.jobs/en/jobs/10513782/software-development-engineer-data-center-builder-tools"
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: LONG + AMAZON_MID for u in urls})
    sources = {"S1": {**res(1, "Software Development Engineer, Data Center Builder Tools", url), "group": "careers", "kind": "company"}}
    search.fetch_full_text(sources, level="new-grad")
    assert sources["S1"]["excluded"] == "not a new-grad posting: asks for 3+ years of experience"
