import { describe, expect, it } from "vitest";
import { checkAll, clientIp, RateLimiter } from "@/lib/ratelimit";
import { request } from "./helpers";

function limiterAt(limit: number, windowMs: number) {
  const clock = { t: 1_000_000 };
  return { clock, limiter: new RateLimiter(limit, windowMs, () => clock.t) };
}

describe("RateLimiter", () => {
  it("allows `limit` requests per window, then says when to retry", () => {
    const { clock, limiter } = limiterAt(3, 60_000);
    expect([limiter.check("a"), limiter.check("a"), limiter.check("a")].every((r) => r.ok)).toBe(true);
    clock.t += 15_000;
    expect(limiter.check("a")).toEqual({ ok: false, retryAfterSeconds: 45 });
  });

  it("slides: a slot frees up when the oldest request leaves the window", () => {
    const { clock, limiter } = limiterAt(2, 60_000);
    limiter.check("a");
    clock.t += 30_000;
    limiter.check("a");
    clock.t += 30_001; // the first request is now outside the window
    expect(limiter.check("a").ok).toBe(true);
    expect(limiter.check("a").ok).toBe(false);
  });

  it("refused requests don't count, and keys are independent", () => {
    const { clock, limiter } = limiterAt(1, 60_000);
    limiter.check("a");
    for (let i = 0; i < 5; i++) limiter.check("a");
    clock.t += 60_001;
    expect(limiter.check("a").ok).toBe(true);
    expect(limiter.check("b").ok).toBe(true);
  });

  it("checkAll stops at the first refusal", () => {
    const strict = limiterAt(1, 60_000).limiter;
    const loose = limiterAt(5, 60_000).limiter;
    checkAll([[strict, "s"], [loose, "ip"]]);
    expect(checkAll([[strict, "s"], [loose, "ip"]]).ok).toBe(false);
    for (let i = 0; i < 4; i++) expect(loose.check("ip").ok).toBe(true); // the refused call didn't use a loose slot
  });
});

describe("clientIp", () => {
  it("takes the first X-Forwarded-For entry", () => {
    expect(clientIp(request("/", { ip: "203.0.113.7, 10.0.0.1" }))).toBe("203.0.113.7");
    expect(clientIp(request("/"))).toBe("unknown");
  });
});
