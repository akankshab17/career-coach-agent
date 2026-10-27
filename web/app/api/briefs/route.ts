import type { NextRequest } from "next/server";
import { callFastapi, FastapiUnavailableError } from "@/lib/fastapi";
import { jsonError, requireSession } from "@/lib/http";
import { checkAll, clientIp, limiters } from "@/lib/ratelimit";
import { BriefSchema, toFastapiBody } from "@/lib/schema";

// POST /api/briefs: session -> validate (zod) -> rate limit -> FastAPI POST /briefs. Answers 202 with the job
// (FastAPI's view: job_id, status, step, ...; reused=true when a recent brief with the same inputs is returned).
export async function POST(request: NextRequest) {
  const session = await requireSession(request);
  if (session instanceof Response) return session;

  const parsed = BriefSchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) return jsonError(400, parsed.error.issues[0]?.message ?? "Invalid request.");

  const limit = checkAll([
    [limiters.briefPerSession, session.sid],
    [limiters.briefPerIp, clientIp(request)],
  ]);
  if (!limit.ok) {
    const minutes = Math.ceil(limit.retryAfterSeconds / 60);
    return jsonError(429, `Too many briefs requested. Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`, {
      "Retry-After": String(limit.retryAfterSeconds),
    });
  }

  try {
    const { status, body } = await callFastapi("/briefs", { method: "POST", json: toFastapiBody(parsed.data) });
    return fromFastapi(status, body);
  } catch (err) {
    if (err instanceof FastapiUnavailableError) return jsonError(503, err.message, { "Retry-After": "10" });
    throw err;
  }
}

/** Passes FastAPI's answer on, except errors that would reveal setup details or that the browser can't act on. */
function fromFastapi(status: number, body: unknown): Response {
  const detail = typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string" ? body.detail : null;
  if (status === 200 || status === 202) return Response.json(body, { status: 202 });
  if (status === 429) return jsonError(429, detail ?? "The daily limit has been reached."); // FastAPI's daily cap / full queue
  if (status === 422) return jsonError(400, "The brief service rejected these inputs."); // our schema should catch these first
  if (status === 401 || status === 403) {
    console.error(`[briefs] FastAPI answered ${status}: check INTERNAL_API_KEY on both servers`);
    return jsonError(502, "The brief service is misconfigured.");
  }
  console.error(`[briefs] FastAPI answered ${status}`);
  return jsonError(502, "The brief service had an error.");
}
