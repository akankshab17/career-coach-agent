import "server-only";
import type { NextRequest } from "next/server";

// Sliding-window rate limits kept in this server's memory. On Vercel each function instance has its own memory, so a
// determined client spread across instances gets a little more: this is a speed bump, not the cap. The real cap is
// FastAPI's MAX_BRIEFS_PER_DAY (shared by everyone, stored in its database).

export type LimitResult = { ok: true } | { ok: false; retryAfterSeconds: number };

export class RateLimiter {
  private hits = new Map<string, number[]>(); // key -> timestamps (ms) of allowed requests inside the window

  constructor(
    readonly limit: number,
    readonly windowMs: number,
    private now: () => number = Date.now,
  ) {}

  /** Counts one request for `key` if it is under the limit; otherwise says how long until a slot frees up. */
  check(key: string): LimitResult {
    const now = this.now();
    const recent = (this.hits.get(key) ?? []).filter((t) => t > now - this.windowMs);
    if (recent.length >= this.limit) {
      this.hits.set(key, recent);
      return { ok: false, retryAfterSeconds: Math.max(1, Math.ceil((recent[0] + this.windowMs - now) / 1000)) };
    }
    recent.push(now);
    this.hits.set(key, recent);
    if (this.hits.size > 10_000) this.prune(now); // keep memory bounded
    return { ok: true };
  }

  reset(): void {
    this.hits.clear();
  }

  private prune(now: number): void {
    for (const [key, times] of this.hits) {
      if (times.every((t) => t <= now - this.windowMs)) this.hits.delete(key);
    }
  }
}

const MINUTE = 60_000;
export const limiters = {
  login: new RateLimiter(10, 15 * MINUTE), // per IP: guessing the access code
  briefPerSession: new RateLimiter(3, 10 * MINUTE),
  briefPerIp: new RateLimiter(10, 60 * MINUTE), // logging in again gives a new session, so also count per IP
};

/** The client's IP. On Vercel, X-Forwarded-For is set by the platform (the first entry is the client). */
export function clientIp(request: NextRequest): string {
  return request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || request.headers.get("x-real-ip") || "unknown";
}

/** Checks every limiter in order and stops at the first refusal. */
export function checkAll(checks: [RateLimiter, string][]): LimitResult {
  for (const [limiter, key] of checks) {
    const result = limiter.check(key);
    if (!result.ok) return result;
  }
  return { ok: true };
}
