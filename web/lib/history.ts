import { z } from "zod";
import type { JobStatus, JobView } from "@/lib/jobs";
import { LEVELS } from "@/lib/schema";

// Recent briefs, remembered in this browser's localStorage (the server lists nothing: PRIVATE_BRIEFS). The JD text
// is never stored, only whether one was used: it can hold personal details. Every storage call is wrapped, because
// storage can be missing or throw (private windows, blocked site data); the history is a convenience, never needed.

export const HISTORY_KEY = "ib_history_v1";
export const MAX_ENTRIES = 20;
export const HISTORY_EVENT = "ib-history-change"; // fired on every write, so useHistory() re-reads

function notify() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(HISTORY_EVENT));
}

const EntrySchema = z.object({
  id: z.string().regex(/^[0-9a-f]{32}$/),
  company: z.string(),
  role: z.string(),
  level: z.enum(LEVELS),
  location: z.string(),
  hasJd: z.boolean(),
  createdAt: z.string(),
  status: z.enum(["queued", "running", "done", "failed", "expired"]),
});

export type HistoryEntry = z.infer<typeof EntrySchema>;
export type HistoryStatus = HistoryEntry["status"];
type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function browserStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function loadHistory(storage: StorageLike | null): HistoryEntry[] {
  try {
    const raw = JSON.parse(storage?.getItem(HISTORY_KEY) ?? "[]");
    if (!Array.isArray(raw)) return [];
    return raw.flatMap((item) => {
      const parsed = EntrySchema.safeParse(item);
      return parsed.success ? [parsed.data] : []; // skip anything malformed instead of losing the rest
    });
  } catch {
    return [];
  }
}

function save(storage: StorageLike | null, entries: HistoryEntry[]): HistoryEntry[] {
  try {
    storage?.setItem(HISTORY_KEY, JSON.stringify(entries.slice(0, MAX_ENTRIES)));
  } catch {
    // full or blocked: the in-memory list is still returned
  }
  notify();
  return entries.slice(0, MAX_ENTRIES);
}

export function entryFromJob(job: JobView): HistoryEntry {
  return {
    id: job.job_id,
    company: job.company,
    role: job.role,
    level: (LEVELS as readonly string[]).includes(job.level) ? (job.level as HistoryEntry["level"]) : "new-grad",
    location: job.location,
    hasJd: job.has_jd,
    createdAt: job.created_at,
    status: job.status,
  };
}

/** Adds or refreshes a brief and moves it to the top. */
export function rememberJob(storage: StorageLike | null, job: JobView): HistoryEntry[] {
  const entry = entryFromJob(job);
  return save(storage, [entry, ...loadHistory(storage).filter((e) => e.id !== entry.id)]);
}

/** Updates a known brief's status in place (unknown ids are ignored: opening a link doesn't reorder history). */
export function setStatus(storage: StorageLike | null, id: string, status: JobStatus | "expired"): HistoryEntry[] {
  const entries = loadHistory(storage);
  if (!entries.some((e) => e.id === id && e.status !== status)) return entries;
  return save(storage, entries.map((e) => (e.id === id ? { ...e, status } : e)));
}

export function forget(storage: StorageLike | null, id: string): HistoryEntry[] {
  return save(storage, loadHistory(storage).filter((e) => e.id !== id));
}

export function clearHistory(storage: StorageLike | null): HistoryEntry[] {
  try {
    storage?.removeItem(HISTORY_KEY);
  } catch {
    // ignore
  }
  notify();
  return [];
}

export function findEntry(storage: StorageLike | null, id: string): HistoryEntry | null {
  return loadHistory(storage).find((e) => e.id === id) ?? null;
}

// ---- "Build again": the form prefilled from a brief's inputs, via the URL ----

export type Prefill = { company: string; role: string; level: HistoryEntry["level"]; location: string; hadJd: boolean };

export function rebuildUrl(entry: Pick<HistoryEntry, "company" | "role" | "level" | "location" | "hasJd">): string {
  const params = new URLSearchParams({ company: entry.company, role: entry.role, level: entry.level, location: entry.location });
  if (entry.hasJd) params.set("jd", "1");
  return `/?${params}`;
}

export function readPrefill(search: string): Prefill | null {
  const params = new URLSearchParams(search);
  const company = params.get("company")?.slice(0, 200);
  const role = params.get("role")?.slice(0, 200);
  if (!company || !role) return null;
  const level = params.get("level");
  return {
    company,
    role,
    level: (LEVELS as readonly string[]).includes(level ?? "") ? (level as Prefill["level"]) : "new-grad",
    location: params.get("location")?.slice(0, 100) || "",
    hadJd: params.get("jd") === "1",
  };
}
