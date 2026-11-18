import type { NextRequest } from "next/server";
import { callFastapi, FastapiUnavailableError } from "@/lib/fastapi";
import { jsonError, requireSession } from "@/lib/http";
import { isJobId } from "@/lib/jobs";
import { upstreamError } from "@/lib/upstream";

// GET /api/briefs/:id: one brief's current state (404 = no longer stored). Used for history and after a lost stream.
export async function GET(request: NextRequest, ctx: RouteContext<"/api/briefs/[id]">) {
  const session = await requireSession(request);
  if (session instanceof Response) return session;
  const { id } = await ctx.params;
  if (!isJobId(id)) return jsonError(404, "This brief is no longer stored on the server.");
  try {
    const { status, body } = await callFastapi(`/briefs/${id}`);
    return status === 200 ? Response.json(body) : upstreamError(status, body, "brief");
  } catch (err) {
    if (err instanceof FastapiUnavailableError) return jsonError(503, err.message, { "Retry-After": "10" });
    throw err;
  }
}
