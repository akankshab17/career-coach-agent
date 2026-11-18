import type { JobView } from "@/lib/jobs";

// What the brief page shows, as a pure reducer over the SSE events (lib/progress.ts) plus connection problems.
// Kept free of React and EventSource so it can be unit-tested.

export type Phase =
  | "connecting" // no answer yet
  | "waking" // the brief service is starting (cold start)
  | "queued" // waiting for another brief to finish (one runs at a time)
  | "running"
  | "done"
  | "failed" // the pipeline failed (job.error says why)
  | "expired" // the server no longer has this brief
  | "error"; // can't continue (misconfiguration, lost connection)

export type ProgressState = { phase: Phase; job: JobView | null; detail: string | null };

export type ProgressAction =
  | { type: "job"; job: JobView }
  | { type: "waking" }
  | { type: "expired" }
  | { type: "error"; detail: string };

export const initialProgress: ProgressState = { phase: "connecting", job: null, detail: null };

export function progressReducer(state: ProgressState, action: ProgressAction): ProgressState {
  switch (action.type) {
    case "job":
      return { phase: action.job.status, job: action.job, detail: null };
    case "waking":
      return isTerminal(state.phase) ? state : { ...state, phase: "waking" }; // keep the job we already know
    case "expired":
      return { phase: "expired", job: state.job, detail: null };
    case "error":
      return { ...state, phase: "error", detail: action.detail };
  }
}

/** Phases after which nothing more will happen, so the stream should be closed. */
export function isTerminal(phase: Phase): boolean {
  return phase === "done" || phase === "failed" || phase === "expired" || phase === "error";
}
