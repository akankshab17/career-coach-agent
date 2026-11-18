import { describe, expect, it, vi } from "vitest";
import {
  clearHistory, findEntry, forget, HISTORY_EVENT, HISTORY_KEY, loadHistory, MAX_ENTRIES, readPrefill, rebuildUrl, rememberJob, setStatus,
} from "@/lib/history";
import type { JobView } from "@/lib/jobs";
import { job } from "./helpers";

class MemoryStorage {
  data = new Map<string, string>();
  getItem(k: string) { return this.data.get(k) ?? null; }
  setItem(k: string, v: string) { this.data.set(k, v); }
  removeItem(k: string) { this.data.delete(k); }
}

const id = (n: number) => n.toString(16).padStart(32, "0");
const j = (n: number, o: Record<string, unknown> = {}) => job({ job_id: id(n), status: "queued", ...o }) as JobView;

describe("history", () => {
  it("remembers briefs newest first, without the JD text", () => {
    const s = new MemoryStorage();
    rememberJob(s, j(1, { company: "Stripe", has_jd: true }));
    rememberJob(s, j(2, { company: "Uber" }));
    const entries = loadHistory(s);
    expect(entries.map((e) => e.company)).toEqual(["Uber", "Stripe"]);
    expect(entries[1]).toMatchObject({ id: id(1), hasJd: true, status: "queued", level: "new-grad", location: "United States" });
    expect(s.getItem(HISTORY_KEY)).not.toContain("jd\":");
  });

  it("re-remembering a brief moves it to the top without duplicating it", () => {
    const s = new MemoryStorage();
    rememberJob(s, j(1));
    rememberJob(s, j(2));
    rememberJob(s, j(1, { status: "done" }));
    expect(loadHistory(s).map((e) => [e.id, e.status])).toEqual([[id(1), "done"], [id(2), "queued"]]);
  });

  it(`keeps at most ${MAX_ENTRIES}`, () => {
    const s = new MemoryStorage();
    for (let n = 1; n <= MAX_ENTRIES + 5; n++) rememberJob(s, j(n));
    const entries = loadHistory(s);
    expect(entries).toHaveLength(MAX_ENTRIES);
    expect(entries[0].id).toBe(id(MAX_ENTRIES + 5));
  });

  it("updates status in place, ignores unknown ids", () => {
    const s = new MemoryStorage();
    rememberJob(s, j(1));
    rememberJob(s, j(2));
    setStatus(s, id(1), "expired");
    setStatus(s, id(9), "done");
    expect(loadHistory(s).map((e) => [e.id, e.status])).toEqual([[id(2), "queued"], [id(1), "expired"]]);
    expect(findEntry(s, id(1))?.status).toBe("expired");
    expect(findEntry(s, id(9))).toBeNull();
  });

  it("forgets one or all", () => {
    const s = new MemoryStorage();
    rememberJob(s, j(1));
    rememberJob(s, j(2));
    expect(forget(s, id(1)).map((e) => e.id)).toEqual([id(2)]);
    expect(clearHistory(s)).toEqual([]);
    expect(loadHistory(s)).toEqual([]);
  });

  it("survives corrupt data, bad entries, and storage that throws or is missing", () => {
    const s = new MemoryStorage();
    s.setItem(HISTORY_KEY, "{not json");
    expect(loadHistory(s)).toEqual([]);
    s.setItem(HISTORY_KEY, JSON.stringify([{ id: "x" }, { ...loadHistoryEntry(), id: id(3) }, 42]));
    expect(loadHistory(s).map((e) => e.id)).toEqual([id(3)]);

    const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("full"); }, removeItem: () => { throw new Error("x"); } };
    expect(loadHistory(throwing)).toEqual([]);
    expect(rememberJob(throwing, j(1))).toHaveLength(1);
    expect(clearHistory(throwing)).toEqual([]);
    expect(rememberJob(null, j(1))).toHaveLength(1);
  });
});

describe("change notifications", () => {
  it("every write tells listeners (useHistory) to re-read", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    const heard = vi.fn();
    target.addEventListener(HISTORY_EVENT, heard);
    const s = new MemoryStorage();
    rememberJob(s, j(1));
    setStatus(s, id(1), "done");
    setStatus(s, id(1), "done"); // no change, no event
    forget(s, id(1));
    clearHistory(s);
    expect(heard).toHaveBeenCalledTimes(4);
  });
});

function loadHistoryEntry() {
  return { id: id(1), company: "A", role: "B", level: "intern", location: "US", hasJd: false, createdAt: "2026-10-27", status: "done" };
}

describe("build again", () => {
  it("round-trips a brief's inputs through the URL", () => {
    const url = rebuildUrl({ company: "AT&T", role: "SWE / Backend", level: "intern", location: "Canada", hasJd: true });
    expect(url.startsWith("/?")).toBe(true);
    expect(readPrefill(url.slice(1))).toEqual({ company: "AT&T", role: "SWE / Backend", level: "intern", location: "Canada", hadJd: true });
  });

  it("ignores incomplete or odd query strings", () => {
    expect(readPrefill("")).toBeNull();
    expect(readPrefill("?company=Acme")).toBeNull();
    expect(readPrefill("?company=Acme&role=SWE&level=ceo")).toMatchObject({ level: "new-grad", location: "", hadJd: false });
  });
});
