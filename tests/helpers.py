"""Shared builders and fakes for the tests."""
import json
import re

TEXTS = {}  # url -> page text served by fake_extract


def res(n, title=None, url=None, content=None, published=None, full_text=None):
    r = {
        "title": title or f"T{n}",
        "url": url or f"https://example.com/{n}",
        "content": content or f"content {n}",
        "published_date": published,
    }
    if full_text:
        r["full_text"] = full_text
    return r


def fake_extract(urls, client=None):
    """Stand-in for Tavily extract: serves pages from TEXTS."""
    return {u: TEXTS[u] for u in urls if u in TEXTS}


def fake_llm(extracted_facts, writer_output, calls=None):
    """Stand-in for llm.generate.

    Extraction calls return extracted_facts[url] (a list of fact dicts); the writer returns writer_output.
    `calls` (a list) records ("extract", url) / ("write", None) in order.
    """
    def generate(system_prompt, user_prompt):
        if "extract facts" in system_prompt:
            url = re.search(r"^URL: (.*)$", user_prompt, re.M).group(1)
            if calls is not None:
                calls.append(("extract", url))
            return json.dumps({"facts": extracted_facts.get(url, [])})
        if calls is not None:
            calls.append(("write", None))
        return writer_output
    return generate


def fact(fact_text, quote, section, source_id="S1", is_forum=False, published=None, stage=None, hedged=False):
    """A grounded fact, as ground() produces it. Interview facts default to stage 'unknown'."""
    return {"fact": fact_text, "exact_quote": quote, "section": section, "is_forum": is_forum,
            "source_id": source_id, "published_date": published, "grounded_in": "full",
            "stage": (stage or "unknown") if section == "interview" else "", "hedged": hedged}
