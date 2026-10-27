import type { NextRequest } from "next/server";
import { callFastapi } from "@/lib/fastapi";
import { jsonError, requireSession } from "@/lib/http";
import { isJobId } from "@/lib/jobs";
import { abortableSleep, briefEvents, formatSse } from "@/lib/progress";

// GET /api/briefs/:id/events: Server-Sent Events with the brief's progress (see lib/progress.ts for the events).
// Each stream ends after ~50s; EventSource reconnects after `retry` ms. Terminal events (job done/failed, expired,
// fatal) tell the browser to stop reconnecting.
export const maxDuration = 60; // seconds; within Vercel Hobby's limit

export async function GET(request: NextRequest, ctx: RouteContext<"/api/briefs/[id]/events">) {
  const session = await requireSession(request);
  if (session instanceof Response) return session;
  const { id } = await ctx.params;
  if (!isJobId(id)) return jsonError(404, "This brief is no longer stored on the server.");

  const abort = new AbortController();
  request.signal.addEventListener("abort", () => abort.abort(), { once: true });
  const events = briefEvents({
    fetchJob: () => callFastapi(`/briefs/${id}`, { timeoutMs: 8_000 }),
    sleep: abortableSleep(abort.signal),
    signal: abort.signal,
  });
  const encoder = new TextEncoder();

  let closed = false; // the browser can cancel while a poll is in flight; never touch a closed controller
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode("retry: 2000\n\n"));
    },
    async pull(controller) {
      let chunk: string | null;
      try {
        const { value, done } = await events.next();
        chunk = done ? null : formatSse(value);
      } catch (err) {
        console.error("[events] stream failed", err);
        chunk = formatSse({ type: "fatal", detail: "The progress stream failed." });
        if (!closed) controller.enqueue(encoder.encode(chunk));
        chunk = null;
      }
      if (closed) return;
      if (chunk === null) {
        closed = true;
        controller.close();
      } else {
        controller.enqueue(encoder.encode(chunk));
      }
    },
    cancel() {
      closed = true;
      abort.abort(); // the browser went away: stop polling FastAPI
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
