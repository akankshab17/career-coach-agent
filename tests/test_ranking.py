"""Which grounded facts the writer gets: specificity, official posting first, near-duplicates, news, personal dates."""
import pytest

import brief
import facts
import graph
import ranking
import search
from brief import RunStats
from helpers import fact

# Facts like the Amazon brief's (the specific ones were left out, the generic ones went in).
OFFICIAL_IV = [fact(t, f"oq{i}", "interview", "S1", level="new-grad") | {"official": True, "source_type": "company-site"} for i, t in
               enumerate(["Amazon interviewers ask about past experiences and specific situations to assess potential performance.",
                          "The Online Assessment places candidates in real Amazon situations so the company can learn more about them.",
                          "Amazon may invite candidates to complete an Online Assessment if its recruiting team wants to proceed."])]
REPORTS = [fact(t, f"rq{i}", "interview", sid, is_experience=True, level="new-grad") | {"source_type": "forum/blog"} for i, (sid, t) in
           enumerate([("S21", "The candidate found the Amazon DSA interview questions surprisingly easy."),
                      ("S21", "The object-oriented design question involved designing a pizza."),
                      ("S21", "The third round's DSA problems involved a variation of LRU Cache and a Top K elements problem."),
                      ("S25", "The third round was entirely behavioral."),
                      ("S25", "The first round included Count Inversions and Course Schedule II."),
                      ("S25", "The second round included leadership-principles questions about exceeding responsibilities and missing a deadline.")])]
MARKETING = [fact(t, f"mq{i}", "role", "S2", level="new-grad") | {"official": True, "source_type": "company-site"} for i, t in
             enumerate(["Amazon seeks self-motivated and resilient individuals who can tackle large-scale challenges.",
                        "Engineering and technical roles involve using powerful technologies to solve challenging and complex issues.",
                        "Amazon invites graduates to browse its latest graduate opportunities for an impactful and dynamic career.",
                        "AWS offers jobs for recent graduates to apply their knowledge and skills after graduation."])]
POSTING = [fact(t, f"pq{i}", "role", "S4", level="new-grad") | {"official": True, "source_type": "official-posting"} for i, t in
           enumerate(["Team placement depends on business needs.",
                      "Applicants must have experience with at least one general-purpose programming language.",
                      "Applicants must have experience with data structures, basic algorithms, or object-oriented design principles.",
                      "Applicants must currently have or be obtaining a bachelor's degree in a related STEM field."])]


def test_specific_facts_score_above_generic_ones():
    s = lambda f: ranking.specificity(f, "Amazon")  # noqa: E731
    assert s(REPORTS[2]) > s(REPORTS[0]) and s(REPORTS[4]) > s(REPORTS[3]) and s(REPORTS[1]) > s(OFFICIAL_IV[0])
    assert s(REPORTS[5]) >= brief.SPECIFIC  # example LP questions
    assert all(s(m) < 0 for m in MARKETING[:3]) and all(s(p) > 0 for p in POSTING[1:])
    assert ranking.specificity(fact("The page includes example behavioral questions and what to expect.", "q", "interview"), "Amazon") < 0


def test_the_writer_gets_the_named_problems_and_the_postings_requirements():
    """Interview: 3 slots, role: 3 slots (limit 11 with 5 company facts). Before: the generic official facts and the
    career site's marketing went in."""
    company = [fact(f"Amazon launched product {i}.", f"cq{i}", "company", f"S{30 + i}") for i in range(5)]
    chosen = brief.select_for_writing(OFFICIAL_IV + REPORTS + MARKETING + POSTING + company, limit=11, level="new-grad",
                                      role="Software Developer", company="Amazon")
    texts = [f["fact"] for f in chosen]
    assert REPORTS[2]["fact"] in texts and REPORTS[4]["fact"] in texts  # LRU Cache / Top K, Course Schedule II
    assert REPORTS[0]["fact"] not in texts and OFFICIAL_IV[0]["fact"] not in texts
    roles = [f for f in chosen if f["section"] == "role"]
    assert {f["source_id"] for f in roles} == {"S4"} and POSTING[0] not in roles  # the posting's requirements, not marketing


def test_role_facts_official_posting_then_company_site_then_the_rest_marketing_last():
    guide = fact("The role requires a bachelor's degree in computer science.", "g", "role", "S16") | {"source_type": "third-party-guide"}
    site = fact("AWS team members gain hands-on experience in the cloud industry.", "s", "role", "S5") | {"source_type": "company-site"}
    order = brief._role_order([guide, MARKETING[1], site] + POSTING, lambda f: ranking.specificity(f, "Amazon"))
    assert [f["source_id"] for f in order][:4] == ["S4"] * 4 and order[4] is site and order[5] is guide and order[-1] is MARKETING[1]
    assert order[:4] == [POSTING[2], POSTING[1], POSTING[3], POSTING[0]]  # within the posting: most specific first


def test_near_duplicates_become_one_fact_that_cites_every_source():
    a = fact("Amazon's graduate roles are for people who recently completed an undergraduate, master's, or doctorate degree.",
             "qa", "role", "S2") | {"source_type": "company-site"}
    b = fact("Amazon's graduate roles are intended for people who recently completed an undergraduate, master's, or doctorate degree.",
             "qb", "role", "S3") | {"source_type": "company-site"}
    c = fact("The interview had 4 rounds.", "qc", "interview", "S7")
    d = fact("The interview had 5 rounds.", "qd", "interview", "S8")
    merged = ranking.merge_near_duplicates([a, b, c, d], better=lambda f: 0, company="Amazon")
    assert len(merged) == 3 and merged[0]["also_sources"] == ["S3"] and merged[0]["merged"] == 2  # c and d differ: 4 vs 5
    chosen = brief.select_for_writing([a, b], company="Amazon")
    assert len(chosen) == 1
    sources = {sid: {"url": f"https://amazon.jobs/{sid}"} for sid in ("S2", "S3")}
    text, cited = brief.render("Graduate roles need a recent degree [F1].", chosen, sources)
    assert text == "Graduate roles need a recent degree [1](https://amazon.jobs/S2)[2](https://amazon.jobs/S3)."
    _, user = brief.build_writer_prompt("Amazon", "SDE", "new-grad", chosen, {**brief.collect_sources({}, "Amazon"), **sources},
                                        brief.date(2026, 10, 21))
    assert "source S2+S3" in user


def test_product_and_tech_news_come_before_market_news_for_engineering_roles():
    stock = fact("Amazon shares rose after analysts raised their price target.", "s", "company", "S6")
    launch = fact("Amazon launched new AI agents for AWS customers.", "l", "company", "S7")
    assert ranking.news_rank(launch, "software") == 0 and ranking.news_rank(stock, "software") == 2
    assert ranking.news_rank(stock, "business") == 1  # other families: no preference
    chosen = brief.select_for_writing([stock, launch], role="Software Developer", company="Amazon")
    assert [f["fact"] for f in chosen] == [stock["fact"], launch["fact"]]  # both fit; order of appearance kept
    many_stock = [fact(f"Amazon stock moved {i} percent after earnings.", f"q{i}", "company", f"S{10 + i}") for i in range(10)]
    chosen = brief.select_for_writing(many_stock + [launch], limit=5, role="Software Developer", company="Amazon")
    assert launch in chosen


# ---- grounding: personal dates ----

@pytest.mark.parametrize("text, rejected", [
    ("The candidate took the online assessment on January 6, 2025.", True),
    ("The recruiter contacted the candidate on Jan 24.", True),
    ("The first interview was rescheduled to the 4th of February.", True),
    ("The candidate received an offer after about six business days.", False),
    ("Applications open in September.", False),
])
def test_personal_dates_from_forum_posts_are_rejected(text, rejected):
    src = {"S1": {"url": "https://www.reddit.com/r/x/comments/1/a", "title": "My Amazon loop", "full_text": text, "kind": "forum"}}
    kept, out = facts.ground([{"fact": text, "exact_quote": text, "section": "timeline", "source_id": "S1"}], src)
    assert bool(out and out[0]["reason"] == "personal-date") is rejected
    official = {"S1": {**src["S1"], "url": "https://amazon.jobs/x", "kind": "company"}}
    assert facts.ground([{"fact": text, "exact_quote": text, "section": "timeline", "source_id": "S1"}], official)[0]  # company dates stay


# ---- the posting location: official postings only ----

def test_indeed_search_pages_are_job_lists():
    assert search.looks_like_job_list("https://www.indeed.com/q-amazon-entry-level-software-engineer-jobs.html")
    assert not search.looks_like_job_list("https://www.indeed.com/viewjob?jk=abc123")


def test_the_location_fact_comes_from_the_official_posting_not_an_aggregator():
    page = "Software Development Engineer\nLocation: Seattle, WA\n" + "Build things. " * 50
    sources = {
        "S4": {"url": "https://www.amazon.jobs/en/jobs/1/sde", "title": "SDE", "full_text": page, "group": "careers",
               "type": "official-posting", "kind": "company"},
        "S27": {"url": "https://www.indeed.com/viewjob?jk=1", "title": "SDE", "full_text": page.replace("Seattle", "Boulder"),
                "group": "posting", "type": "job-aggregator", "kind": "other"},
    }
    state = {"stats": RunStats(), "candidates": [], "sources": sources, "facts": [], "location_done": False, "location": "United States",
             "company": "Amazon"}
    out = graph.ground_node(state)
    loc = [f for f in out["facts"] if f.get("pinned")]
    assert len(loc) == 1 and loc[0]["source_id"] == "S4" and "Seattle" in loc[0]["fact"]
