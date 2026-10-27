"use client";

import { useRouter } from "next/navigation";
import { useEffect, useReducer } from "react";
import type { JobView } from "@/lib/jobs";
import { initialProgress, isTerminal, progressReducer, type ProgressAction, type ProgressState } from "@/lib/progressState";

/**
 * Follows a brief's progress over SSE. EventSource reconnects by itself when a stream ends normally (~50s); when the
 * connection is refused outright (401, 404, server down) it gives up, so this asks GET /api/briefs/:id why and acts.
 */
export function useBriefProgress(id: string): ProgressState {
  const [state, dispatch] = useReducer(progressReducer, initialProgress);
  const router = useRouter();

  useEffect(() => {
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let terminal = false;

    const handle = (action: ProgressAction) => {
      dispatch(action);
      const phase = action.type === "job" ? action.job.status : action.type;
      if (isTerminal(phase)) {
        terminal = true;
        source?.close(); // otherwise EventSource would reconnect forever
      }
    };

    const open = () => {
      source = new EventSource(`/api/briefs/${id}/events`);
      source.addEventListener("job", (e) => handle({ type: "job", job: (JSON.parse(e.data) as { job: JobView }).job }));
      source.addEventListener("waking", () => handle({ type: "waking" }));
      source.addEventListener("expired", () => handle({ type: "expired" }));
      source.addEventListener("fatal", (e) => handle({ type: "error", detail: (JSON.parse(e.data) as { detail: string }).detail }));
      source.addEventListener("error", () => {
        if (source?.readyState === EventSource.CLOSED && !terminal && !stopped) void diagnose(); // refused, not just ended
      });
    };

    const diagnose = async () => {
      try {
        const res = await fetch(`/api/briefs/${id}`, { cache: "no-store" });
        if (stopped) return;
        if (res.status === 401) return router.replace(`/login?next=${encodeURIComponent(`/briefs/${id}`)}`);
        if (res.status === 404) return handle({ type: "expired" });
        if (res.ok) handle({ type: "job", job: (await res.json()) as JobView });
        if (res.status === 503) handle({ type: "waking" });
        if (!res.ok && res.status !== 503) {
          const body = (await res.json().catch(() => null)) as { detail?: string } | null;
          return handle({ type: "error", detail: body?.detail ?? `The server answered ${res.status}.` });
        }
      } catch {
        if (stopped) return;
        handle({ type: "waking" }); // offline or the server is down: keep trying
      }
      if (!terminal && !stopped) retryTimer = setTimeout(open, 3_000);
    };

    open();
    return () => {
      stopped = true;
      clearTimeout(retryTimer);
      source?.close();
    };
  }, [id, router]);

  return state;
}
