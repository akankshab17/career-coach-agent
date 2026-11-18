import "server-only";
import type { NextRequest } from "next/server";
import { ConfigError, SESSION_COOKIE, verifySessionToken, type Session } from "@/lib/session";

/** A JSON error in the same shape FastAPI uses ({detail}), so the browser handles both alike. */
export function jsonError(status: number, detail: string, headers?: HeadersInit): Response {
  return Response.json({ detail }, { status, headers });
}

/**
 * Route handlers call this even though proxy.ts already checks the cookie: if a matcher change ever let a request
 * past the proxy, the handler still refuses it. Returns the session, or the error response to send.
 */
export async function requireSession(request: NextRequest): Promise<Session | Response> {
  try {
    const session = await verifySessionToken(request.cookies.get(SESSION_COOKIE)?.value);
    return session ?? jsonError(401, "Please log in.");
  } catch (err) {
    if (err instanceof ConfigError) return jsonError(500, "The server is not configured.");
    throw err;
  }
}
