"use client";

import Link from "next/link";
import { useEffect } from "react";
import { browserStorage, clearHistory, forget, loadHistory, rebuildUrl, setStatus, type HistoryEntry, type HistoryStatus } from "@/lib/history";
import type { JobView } from "@/lib/jobs";
import { useHistory } from "./useHistory";

// Recent briefs from this browser. On load, each one's status is refreshed from the server; briefs the server no
// longer has (its storage is wiped when it restarts) are shown as expired, with "Build again".

const MAX_CHECKS = 10;
const RETRIES = 6;
const RETRY_MS = 5_000;

export function History() {
  const entries = useHistory(); // null on the server and before hydration

  // Once per visit: ask the server about each remembered brief (writes go through the store, so the list updates).
  // A 503 or network error usually means the server is waking up, so those are retried for about 30 seconds.
  useEffect(() => {
    const storage = browserStorage();
    const controller = new AbortController();
    const timers: ReturnType<typeof setTimeout>[] = [];
    const check = async (id: string, attempt: number) => {
      try {
        const res = await fetch(`/api/briefs/${id}`, { cache: "no-store", signal: controller.signal });
        if (res.status === 404) return setStatus(storage, id, "expired");
        if (res.ok) return setStatus(storage, id, ((await res.json()) as JobView).status);
        if (res.status !== 503) return; // e.g. logged out: leave it
      } catch {
        if (controller.signal.aborted) return;
      }
      if (attempt < RETRIES) timers.push(setTimeout(() => void check(id, attempt + 1), RETRY_MS));
    };
    for (const e of loadHistory(storage).filter((e) => e.status !== "expired").slice(0, MAX_CHECKS)) void check(e.id, 1);
    return () => {
      controller.abort();
      timers.forEach(clearTimeout);
    };
  }, []);

  if (!entries || entries.length === 0) return null;

  return (
    <section aria-labelledby="history-heading">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 id="history-heading" className="font-semibold tracking-tight">
          Recent briefs
        </h2>
        <button onClick={() => clearHistory(browserStorage())} className="text-sm text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200">
          Clear
        </button>
      </div>
      <ul className="card divide-y divide-zinc-200 overflow-hidden dark:divide-zinc-800">
        {entries.map((e) => (
          <li key={e.id} className="group relative flex items-center gap-3 px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-800/40">
            <div className="min-w-0 flex-1">
              {e.status === "expired" ? (
                <p className="truncate font-medium text-zinc-500">
                  {e.company} · {e.role}
                </p>
              ) : (
                <Link href={`/briefs/${e.id}`} className="block truncate font-medium after:absolute after:inset-0">
                  {e.company} · {e.role}
                </Link>
              )}
              <p className="mt-0.5 truncate text-sm text-zinc-500">
                {LEVEL_LABELS[e.level]} · {e.location} · {formatDate(e.createdAt)}
              </p>
            </div>
            <StatusBadge status={e.status} />
            {e.status === "expired" && (
              <Link href={rebuildUrl(e)} className="relative z-10 shrink-0 text-sm font-medium underline underline-offset-2">
                Build again
              </Link>
            )}
            <button
              onClick={() => forget(browserStorage(), e.id)}
              aria-label={`Remove ${e.company} ${e.role} from history`}
              className="relative z-10 -mr-1 shrink-0 rounded p-1 text-zinc-400 hover:bg-zinc-200 hover:text-zinc-700 dark:hover:bg-zinc-700 dark:hover:text-zinc-200"
            >
              <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
                <path d="M4 4l8 8M12 4l-8 8" />
              </svg>
            </button>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-zinc-500">Saved in this browser only. Briefs expire when the server restarts after a period of inactivity.</p>
    </section>
  );
}

const LEVEL_LABELS: Record<HistoryEntry["level"], string> = { intern: "Intern", "new-grad": "New grad", experienced: "Experienced" };

const BADGES: Record<HistoryStatus, [string, string]> = {
  done: ["Ready", "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-900"],
  running: ["Building", "bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-950/40 dark:text-blue-300 dark:ring-blue-900"],
  queued: ["Waiting", "bg-zinc-100 text-zinc-700 ring-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:ring-zinc-700"],
  failed: ["Failed", "bg-red-50 text-red-700 ring-red-200 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-900"],
  expired: ["Expired", "bg-zinc-100 text-zinc-500 ring-zinc-200 dark:bg-zinc-800 dark:text-zinc-400 dark:ring-zinc-700"],
};

function StatusBadge({ status }: { status: HistoryStatus }) {
  const [label, cls] = BADGES[status];
  return <span className={`hidden shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ring-1 sm:inline ${cls}`}>{label}</span>;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
