import "server-only";
import { FastapiUnavailableError, type FastapiResponse } from "@/lib/fastapi";
import { isFinished, type JobView } from "@/lib/jobs";
import { detailOf } from "@/lib/upstream";

// What the SSE route (app/api/briefs/[id]/events) streams: FastAPI has no push, so this polls GET /briefs/{id} and
// emits an event only when something changed. One stream lasts at most maxMs (Vercel caps function time); the
// browser's EventSource then reconnects by itself and polling resumes.

export type BriefEvent =
  | { type: "job"; job: JobView } // new status/step/log line, or the finished job (status done|failed)
  | { type: "waking" } // FastAPI not answering: probably a cold start (Azure scales to zero)
  | { type: "expired" } // FastAPI has no such job: its disk was wiped by a restart, or the id is wrong
  | { type: "fatal"; detail: string } // won't fix itself (misconfiguration, server error)
  | { type: "ping" }; // keep-alive, so proxies don't close an idle connection

export type PollOptions = {
  fetchJob: () => Promise<FastapiResponse>;
  sleep: (ms: number) => Promise<void>;
  now?: () => number;
  intervalMs?: number;
  maxMs?: number;
  signal?: AbortSignal;
};

const MAX_UPSTREAM_ERRORS = 3; // a 5xx can be a blip; three in a row is reported

export async function* briefEvents(opts: PollOptions): AsyncGenerator<BriefEvent> {
  const now = opts.now ?? Date.now;
  const intervalMs = opts.intervalMs ?? 2_000;
  const deadline = now() + (opts.maxMs ?? 50_000);
  let last = "";
  let upstreamErrors = 0;

  while (!opts.signal?.aborted) {
    let res: FastapiResponse | null = null;
    try {
      res = await opts.fetchJob();
    } catch (err) {
      if (!(err instanceof FastapiUnavailableError)) throw err;
    }

    if (res === null) {
      yield { type: "waking" };
    } else if (res.status === 200) {
      upstreamErrors = 0;
      const job = res.body as JobView;
      const key = `${job.status}|${job.step}|${job.steps.length}`;
      if (key !== last) {
        last = key;
        yield { type: "job", job };
        if (isFinished(job)) return;
      } else {
        yield { type: "ping" };
      }
    } else if (res.status === 404) {
      yield { type: "expired" };
      return;
    } else if (res.status === 401 || res.status === 403) {
      console.error(`[events] FastAPI answered ${res.status}: check INTERNAL_API_KEY on both servers`);
      yield { type: "fatal", detail: "The brief service is misconfigured." };
      return;
    } else if (++upstreamErrors >= MAX_UPSTREAM_ERRORS) {
      yield { type: "fatal", detail: detailOf(res.body) ?? `The brief service had an error (${res.status}).` };
      return;
    }

    if (now() + intervalMs >= deadline) return; // the browser reconnects
    await opts.sleep(intervalMs);
  }
}

/** One event in text/event-stream format. A ping is a comment line, which EventSource ignores. */
export function formatSse(event: BriefEvent): string {
  if (event.type === "ping") return ": ping\n\n";
  const { type, ...data } = event;
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** A sleep that ends early when the browser disconnects. */
export function abortableSleep(signal: AbortSignal): (ms: number) => Promise<void> {
  return (ms) =>
    new Promise((resolve) => {
      const timer = setTimeout(resolve, ms);
      signal.addEventListener("abort", () => (clearTimeout(timer), resolve()), { once: true });
    });
}
