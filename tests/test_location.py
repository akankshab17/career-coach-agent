import pytest

import facts
import location
import search
from helpers import res

MUSE_WUHAN = (
    "# 2027 Early Career - Electronic Systems Engineer - Software\n\nCummins Inc. is headquartered in Columbus, Indiana. "
    "This job is located in Wuhan, China. Work with the Wuhan engineering centre in China on controls software. " + "x " * 600
)
STRIPE_US = "Software Engineer, New Grad\n\nLocations: New York, Seattle, South San Francisco\n\nYou will build APIs. " + "x " * 600


# ---- classify ----

@pytest.mark.parametrize("text, wanted, expected", [
    ("Location: Seattle, WA", "United States", "match"),
    ("Locations: New York, Seattle, South San Francisco", "United States", "match"),
    ("Join us in Mountain View, California.", "USA", "match"),
    ("Remote - US", "us", "match"),
    ("Location: Wuhan, China", "United States", "other"),
    ("Bangalore, India", "United States", "other"),
    ("Bangalore, India", "India", "match"),
    ("Remote", "United States", "unknown"),
    ("", "United States", "unknown"),
    (MUSE_WUHAN, "United States", "other"),  # China outweighs the Columbus, Indiana boilerplate
    (MUSE_WUHAN, "China", "match"),
    ("Anywhere you like", None, "unknown"),  # no location wanted: nothing to judge
    ("Office in Austin, TX", "Austin, TX", "match"),  # a city, not a country
    ("Office in Toronto, Canada", "Austin, TX", "other"),
])
def test_classify_location(text, wanted, expected):
    assert location.classify_location(text, wanted) == expected


def test_a_location_line_beats_boilerplate_elsewhere_on_the_page():
    text = "Job Location: Pune, India\n\nCummins has offices in Columbus, Indiana; Seattle, WA; and Houston, TX. " * 3
    assert location.classify_location(text, "United States") == "other"


def test_country_names_and_synonyms():
    assert location.canonical_country("USA") == location.canonical_country("United States") == "united states"
    assert location.canonical_country("UK") == "united kingdom"
    assert location.canonical_country("Austin, TX") is None


# ---- find_location ----

def test_find_location_from_a_location_line_and_from_place_names():
    found = location.find_location("Intro\nLocation: Wuhan, China\nMore", "United States")
    assert found == {"place": "Wuhan, China", "quote": "Wuhan, China", "class": "other"}

    found = location.find_location(MUSE_WUHAN, "United States")
    assert found["class"] == "other" and "Wuhan" in found["place"] and "China" in found["place"]
    assert found["quote"] in MUSE_WUHAN  # always verbatim from the page

    found = location.find_location(STRIPE_US, "United States")
    assert found["place"] == "New York, Seattle, South San Francisco" and found["class"] == "match"
    assert location.find_location("No places in this text at all.", "United States") is None


# ---- ranking + resolving postings ----

def posting(n, title, url, content=""):
    return res(n, title, url, content=content or f"snippet {n}")


def test_postings_in_the_location_come_first_and_other_countries_are_dropped_when_one_exists():
    china = posting(1, "Software Engineer, New Grad", "https://jobs.example.com/careers/1", "New grad role in Wuhan, China")
    us = posting(2, "Software Engineer, New Grad", "https://jobs.example.com/careers/2", "New grad role in Seattle, WA")
    unknown = posting(3, "Software Engineer, New Grad", "https://jobs.example.com/careers/3", "New grad role, great team")
    ranked = search.rank_postings([china, unknown, us], "Acme", "Software Engineer", "new-grad", "United States")
    assert [r["url"] for r in ranked] == [us["url"], unknown["url"]]  # China is dropped: a US posting exists
    assert ranked[0]["location_class"] == "match" and ranked[1]["location_class"] == "unknown"

    only_other = search.rank_postings([china, unknown], "Acme", "Software Engineer", "new-grad", "United States")
    assert [r["url"] for r in only_other] == [unknown["url"], china["url"]]  # unknown before other; other kept as a last resort

    no_location = search.rank_postings([china, us], "Acme", "Software Engineer", "new-grad")
    assert [r["url"] for r in no_location] == [china["url"], us["url"]]  # without a location, order is unchanged


def test_resolve_posting_skips_a_page_in_another_country_when_a_better_one_exists(monkeypatch, capsys):
    pages = {"https://acme.com/jobs/1": MUSE_WUHAN, "https://acme.com/jobs/2": STRIPE_US}
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: pages[u] for u in urls})
    out = search.resolve_posting([res(1, url="https://acme.com/jobs/1"), res(2, url="https://acme.com/jobs/2")], "United States")
    assert out[0]["url"] == "https://acme.com/jobs/2" and out[0]["location_class"] == "match"
    assert "not clearly in United States (looks other)" in capsys.readouterr().err


def test_resolve_posting_uses_another_country_only_as_a_last_resort(monkeypatch):
    pages = {"https://acme.com/jobs/1": MUSE_WUHAN, "https://acme.com/jobs/2": "Great job, no place named. " * 40}
    monkeypatch.setattr(search, "extract", lambda urls, client=None: {u: pages[u] for u in urls})
    cands = [res(1, url="https://acme.com/jobs/1"), res(2, url="https://acme.com/jobs/2")]
    assert search.resolve_posting(cands, "United States")[0]["url"] == "https://acme.com/jobs/2"  # unknown beats other
    only_china = search.resolve_posting(cands[:1], "United States")
    assert only_china[0]["url"] == "https://acme.com/jobs/1" and only_china[0]["location_class"] == "other"
    assert search.resolve_posting(cands, None)[0]["url"] == "https://acme.com/jobs/1"  # no location wanted: first valid page


def test_posting_queries_include_the_location(monkeypatch):
    queries = []
    monkeypatch.setattr(search, "search", lambda q, client=None, **kw: queries.append(q) or [])
    search.gather_research("Acme", "SRE", "new-grad", None, location="Canada")
    assert any("job posting Canada" in q for q in queries)
    queries.clear()
    search.search_more("Acme", "SRE", "new-grad", "role", set(), location="India")
    assert any(q.endswith("India") for q in queries)


# ---- the location fact ----

def test_location_fact_is_grounded_by_construction_and_pinned():
    src = {"url": "https://acme.com/jobs/1", "full_text": MUSE_WUHAN, "published_date": None}
    fact = facts.location_fact("S4", src, "United States")
    assert fact["pinned"] and fact["section"] == "role" and fact["source_id"] == "S4"
    assert fact["fact"].startswith("Posting location: ") and fact["fact"].endswith("(outside United States).")
    assert fact["exact_quote"] in MUSE_WUHAN

    us = facts.location_fact("S1", {"url": "u", "full_text": STRIPE_US}, "United States")
    assert us["fact"] == "Posting location: New York, Seattle, South San Francisco."  # no suffix when it's in the location
    assert facts.location_fact("S1", {"url": "u", "full_text": "nothing here " * 50}, "United States") is None
    assert facts.location_fact("S1", {"url": "u", "content": "snippet only"}, "United States") is None


def test_source_kind_blogs():
    assert search.source_kind("https://medium.com/@x/my-interview", "Cummins") == "blog"
    assert search.source_kind("https://foo.substack.com/p/1", "Cummins") == "blog"
    assert search.source_kind("https://www.reddit.com/r/x/1", "Cummins") == "forum"
