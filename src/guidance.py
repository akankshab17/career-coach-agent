"""General interview-prep guidance per role family, for briefs where no company-specific interview or role facts were found.

Written here in code (never by the LLM), so it can't invent company facts; the brief labels it
"General guidance (no company-specific data found)" so nobody mistakes it for something a source said.
"""
from __future__ import annotations

from search import role_family

LABEL = "General guidance (no company-specific data found)"

FAMILY_NAME = {
    "software": "software engineering", "product": "product management", "data": "data and analytics",
    "hardware": "hardware engineering", "design": "design", "security": "security", "business": "business",
}

PREP = {
    "software": [
        "Practise coding problems on arrays, strings, hash maps, trees and graphs, explaining your approach out loud as you go.",
        "State the time and space complexity of each solution and test it on edge cases before calling it done.",
        "Prepare two or three stories about projects you built: what you did, what went wrong, and what you learned.",
        "Review the languages and tools on your résumé, since interviewers often ask about them.",
    ],
    "product": [
        "Practise product-sense questions: pick a product you use, define its users and their problems, then propose and prioritise improvements.",
        "Practise estimation and metrics questions, such as sizing a market or deciding how to measure whether a feature worked.",
        "Prepare stories about leading without authority, making a trade-off, and using data to change a decision.",
        "Learn the company's main products well enough to discuss who uses them and why.",
    ],
    "data": [
        "Practise SQL: joins, aggregations, window functions, and reading a schema you have not seen before.",
        "Review statistics basics: distributions, hypothesis tests, A/B test design, and common pitfalls such as selection bias.",
        "Prepare to walk through a past analysis or model end to end: the question, the data, the method, the result and its impact.",
        "Practise explaining a technical result to a non-technical audience in a few sentences.",
    ],
    "hardware": [
        "Review digital logic, timing, and the fundamentals behind the circuits or systems on your résumé.",
        "Be ready to explain a lab or project design: the requirements, your choices, your measurements, and what you would change.",
        "Practise reasoning through a debugging scenario step by step, out loud.",
    ],
    "design": [
        "Prepare a portfolio walkthrough of two or three projects: the problem, your process, the trade-offs, and the outcome.",
        "Practise a timed design challenge, narrating your reasoning as you work.",
        "Be ready to explain how research or feedback changed one of your designs.",
    ],
    "security": [
        "Review core concepts: authentication, common web vulnerabilities, networking basics, and cryptography fundamentals.",
        "Practise threat-modelling a simple system out loud: assets, attackers, entry points, and mitigations.",
        "Prepare to discuss a security project, CTF, or incident you worked on.",
    ],
    "business": [
        "Practise structured case questions: clarify the goal, break the problem down, and state your assumptions.",
        "Prepare examples of results you achieved and how you measured them.",
        "Learn the company's main products and customers well enough to discuss them.",
    ],
    None: [
        "Prepare two or three stories about your projects or experience: the situation, what you did, and the result.",
        "Match each requirement you expect for the role to an example from your background.",
        "Practise explaining your experience clearly and briefly, out loud.",
    ],
}

QUESTIONS = {
    "software": [
        "What does someone joining at my level typically work on in their first few months?",
        "How does the team review code and decide what to build next?",
        "What does onboarding and mentorship look like on this team?",
    ],
    "product": [
        "How do product managers at my level split their time between discovery, execution and working with engineering?",
        "How does the team decide what to build next, and how does it measure whether it worked?",
        "What mentorship or program structure is there for early-career product managers?",
    ],
    "data": [
        "What data and tools does the team work with day to day?",
        "How do analyses or models turn into decisions or product changes here?",
        "How is a new team member's work reviewed and supported?",
    ],
    "hardware": [
        "What stage of the product cycle would I join, and what does a typical project look like?",
        "What tools and lab equipment does the team use?",
        "How are new engineers mentored?",
    ],
    "design": [
        "How do designers work with product managers and engineers here?",
        "How is design work critiqued and reviewed?",
        "What would success look like in my first months?",
    ],
    "security": [
        "How does the team split its time between building tools, reviews, and incident response?",
        "How does the security team work with product teams?",
        "How are new team members onboarded?",
    ],
    "business": [
        "What does a typical week look like in this role?",
        "How is success measured for someone in their first year?",
        "What training or mentorship is available?",
    ],
    None: [
        "What does someone joining at my level typically work on first?",
        "How is success measured in this role?",
        "What onboarding and mentorship are available?",
    ],
}


def family_name(role: str) -> str:
    family = role_family(role)
    return f"{FAMILY_NAME[family]} roles" if family else "roles like this one"


def section_text(kind: str, role: str) -> str:
    """The body of section 4 ('prep') or 5 ('questions'): the label line, then bullets."""
    family = role_family(role)
    items = (PREP if kind == "prep" else QUESTIONS).get(family) or (PREP if kind == "prep" else QUESTIONS)[None]
    return f"**{LABEL}**, typical for {family_name(role)}:\n" + "\n".join(f"- {i}" for i in items)
