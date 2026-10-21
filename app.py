"""Streamlit front end for the brief API: streamlit run app.py  (start the API first; see README "Web app").

A thin client: everything goes through the API at BRIEF_API_URL (default http://localhost:8000). If the API needs an
access code (APP_ACCESS_CODE on the server), the app asks for it once per browser session.
"""
from __future__ import annotations

import os
import re
import time

import httpx
import streamlit as st

API_URL = os.getenv("BRIEF_API_URL", "http://localhost:8000").rstrip("/")
LEVELS = {"new-grad": "New grad", "intern": "Intern", "experienced": "Experienced"}
STAGES = ("search", "fetch", "extract", "ground", "write", "finalize")  # what the progress bar walks through
STAGE_OF = {"coverage": "ground", "search_more": "search", "done": "finalize"}  # the other graph steps, placed on the bar
STAGE_LABEL = {"search": "Searching the web", "fetch": "Reading pages", "extract": "Extracting facts",
               "ground": "Checking facts against the sources", "write": "Writing the brief", "finalize": "Checking every sentence"}
POLL_SECONDS = 1.0


class APIError(Exception):
    pass


def call(method: str, path: str, **kw) -> dict | list:
    headers = {"X-Access-Code": st.session_state["access_code"]} if st.session_state.get("access_code") else {}
    try:
        r = httpx.request(method, f"{API_URL}{path}", headers=headers, timeout=20, **kw)
    except httpx.HTTPError as exc:
        raise APIError(f"Can't reach the API at {API_URL} ({type(exc).__name__}). Is it running?") from exc
    if r.status_code == 401:
        st.session_state.pop("access_code", None)  # ask again
        raise APIError("The access code was not accepted.")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise APIError(f"API error {r.status_code}: {detail}")
    return r.json()


def require_access() -> None:
    """Ask for the access code once per session when the API wants one; stop the page until it is right."""
    try:
        health = call("GET", "/health")
    except APIError as exc:
        st.error(str(exc))
        st.stop()
    if not health.get("access_code_required") or st.session_state.get("access_ok"):
        return
    st.title("Interview brief")
    with st.form("access"):
        code = st.text_input("Access code", type="password", help="Ask the person who runs this app.")
        if st.form_submit_button("Continue") and code:
            st.session_state["access_code"] = code
            try:
                call("GET", "/auth")
                st.session_state["access_ok"] = True
                st.rerun()
            except APIError as exc:
                st.error(str(exc))
    st.stop()


def open_job(job_id: str) -> None:
    st.session_state["job_id"] = job_id
    st.query_params["job"] = job_id


def sidebar() -> None:
    st.sidebar.header("Recent briefs")
    try:
        jobs = call("GET", "/briefs", params={"limit": 20})
    except APIError as exc:
        st.sidebar.warning(str(exc))
        return
    if not jobs:
        st.sidebar.caption("None yet.")
    for job in jobs:
        status = "" if job["status"] == "done" else f" ({job['status']})"
        label = f"{job['company']} · {job['role']} · {LEVELS.get(job['level'], job['level'])}{status}"
        if st.sidebar.button(label, key=f"job-{job['job_id']}", help=f"{job['location']}, {job['created_at'][:16].replace('T', ' ')} UTC",
                             use_container_width=True):
            open_job(job["job_id"])
            st.rerun()


def request_form() -> None:
    with st.form("brief"):
        c1, c2 = st.columns(2)
        company = c1.text_input("Company", placeholder="e.g. Salesforce")
        role = c2.text_input("Role", placeholder="e.g. Associate Product Manager")
        c3, c4 = st.columns(2)
        level = c3.selectbox("Level", list(LEVELS), format_func=LEVELS.get)
        location = c4.text_input("Location", value="United States")
        jd = st.text_area("Job description (optional)", height=140, placeholder="Paste the posting to ground the role section in it.")
        force = st.checkbox("Run again even if a recent brief exists", value=False)
        if st.form_submit_button("Build brief", type="primary"):
            if not company.strip() or not role.strip():
                st.error("Company and role are required.")
                return
            try:
                job = call("POST", "/briefs", json={"company": company, "role": role, "level": level, "location": location or "United States",
                                                    "jd": jd or None, "force": force})
            except APIError as exc:
                st.error(str(exc))
                return
            open_job(job["job_id"])
            st.rerun()  # redraw from the top so the sidebar already lists the new brief while it runs


def stage_index(step: str | None) -> int:
    stage = STAGE_OF.get(step or "", step)
    return STAGES.index(stage) if stage in STAGES else 0


def show_progress(job: dict, box) -> None:
    with box.container():
        if job["status"] == "queued":
            st.info("Waiting in the queue...")
            return
        i = stage_index(job.get("step"))
        st.progress((i + 0.5) / len(STAGES), text=f"{STAGE_LABEL[STAGES[i]]}..."
                    + (" (second search round)" if job.get("step") == "search_more" else ""))
        st.caption(" → ".join(f"**{s}**" if n == i else s for n, s in enumerate(STAGES)))
        if job.get("steps"):
            st.caption(f"`{job['steps'][-1]}`")


def file_name(job: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['role']}-{job['level']}".lower()).strip("-")
    return f"{slug}.md"


def show_job(job_id: str) -> None:
    box = st.empty()
    try:
        job = call("GET", f"/briefs/{job_id}")
        while job["status"] in ("queued", "running"):
            show_progress(job, box)
            time.sleep(POLL_SECONDS)
            job = call("GET", f"/briefs/{job_id}")
    except APIError as exc:
        box.error(str(exc))
        return
    box.empty()

    if job["status"] == "failed":
        st.error(f"The brief could not be built: {job['error']}")
    else:
        if job.get("reused"):
            st.caption(f"Reused a brief from {job['finished_at'][:10]} (same inputs, under 7 days old). Tick 'Run again' to rebuild it.")
        st.download_button("Download brief (.md)", job["brief_markdown"], file_name=file_name(job), mime="text/markdown")
        st.markdown(job["brief_markdown"])
    summary = job.get("debug_summary")
    if summary:
        with st.expander("Run details"):
            cols = st.columns(4)
            cols[0].metric("Sources", summary["sources"])
            cols[1].metric("Facts grounded", summary["facts_grounded"])
            cols[2].metric("Facts rejected", summary["facts_rejected"])
            cols[3].metric("Sentences dropped", summary["sentences_dropped"])
            st.write("Models:", ", ".join(f"{task}: {', '.join(m)}" for task, m in summary["llm"].items()) or "none")
            if summary.get("cost_usd"):
                st.write(f"Cost: ${summary['cost_usd']:.4f}")
            st.code("\n".join(summary["steps"]), language=None)


def main() -> None:
    st.set_page_config(page_title="Interview brief", layout="wide")
    require_access()
    sidebar()
    st.title("Interview brief")
    st.caption("Every sentence in a brief cites a source and was checked against that source's text.")
    request_form()
    job_id = st.session_state.get("job_id") or st.query_params.get("job")
    if job_id:
        st.divider()
        show_job(job_id)


main()
