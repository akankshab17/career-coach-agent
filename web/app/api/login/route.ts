import { NextResponse, type NextRequest } from "next/server";
import { jsonError } from "@/lib/http";
import { clientIp, limiters } from "@/lib/ratelimit";
import { LoginSchema } from "@/lib/schema";
import { accessCodeMatches, ConfigError, createSessionToken, SESSION_COOKIE, sessionCookieOptions } from "@/lib/session";

// POST /api/login {code}: right code -> 200 and a fresh session cookie; wrong code -> 401; too many tries -> 429.
export async function POST(request: NextRequest) {
  const limit = limiters.login.check(clientIp(request)); // counts every attempt, right or wrong
  if (!limit.ok) {
    return jsonError(429, "Too many login attempts. Try again later.", { "Retry-After": String(limit.retryAfterSeconds) });
  }
  const parsed = LoginSchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) return jsonError(400, parsed.error.issues[0]?.message ?? "Invalid request.");
  try {
    if (!accessCodeMatches(parsed.data.code)) return jsonError(401, "Wrong access code.");
    const response = NextResponse.json({ ok: true });
    response.cookies.set(SESSION_COOKIE, await createSessionToken(), sessionCookieOptions);
    return response;
  } catch (err) {
    if (err instanceof ConfigError) {
      console.error(`[login] ${err.message}`);
      return jsonError(500, "The server is not configured.");
    }
    throw err;
  }
}
