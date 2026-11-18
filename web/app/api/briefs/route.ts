import type { NextRequest } from "next/server";
import { callFastapi, FastapiUnavailableError } from "@/lib/fastapi";
import { jsonError, requireSession } from "@/lib/http";
import { checkAll, clientIp, limiters } from "@/lib/ratelimit";
import { BriefSchema, toFastapiBody } from "@/lib/schema";
import { upstreamError } from "@/lib/upstream";

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
    if (status === 200 || status === 202) return Response.json(body, { status: 202 });
    return upstreamError(status, body, "briefs");
  } catch (err) {
    if (err instanceof FastapiUnavailableError) return jsonError(503, err.message, { "Retry-After": "10" });
    throw err;
  }
}
