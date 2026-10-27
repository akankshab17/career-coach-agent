import { describe, expect, it, vi } from "vitest";
import { safeNextPath } from "@/lib/paths";
import { accessCodeMatches, ConfigError, createSessionToken, SESSION_TTL_SECONDS, verifySessionToken } from "@/lib/session";
import { CODE, configure } from "./helpers";

describe("session tokens", () => {
  it("round-trips a session with a random id and a 7-day expiry", async () => {
    configure();
    const now = new Date("2026-10-27T12:00:00Z");
    const session = await verifySessionToken(await createSessionToken(now), now);
    expect(session?.sid).toMatch(/^[0-9a-f-]{36}$/);
    expect(session?.exp).toBe(now.getTime() / 1000 + SESSION_TTL_SECONDS);
    const other = await verifySessionToken(await createSessionToken(now), now);
    expect(other?.sid).not.toBe(session?.sid);
  });

  it("rejects expired, tampered, foreign and missing tokens", async () => {
    configure();
    const now = new Date("2026-10-27T12:00:00Z");
    const token = await createSessionToken(now);
    const later = new Date(now.getTime() + (SESSION_TTL_SECONDS + 1) * 1000);
    expect(await verifySessionToken(token, later)).toBeNull();

    const [header, payload, sig] = token.split(".");
    const forged = Buffer.from(JSON.stringify({ sid: "admin", exp: 9999999999 })).toString("base64url");
    expect(await verifySessionToken(`${header}.${forged}.${sig}`, now)).toBeNull();
    expect(await verifySessionToken(`${header}.${payload}.x${sig.slice(1)}`, now)).toBeNull();

    vi.stubEnv("SESSION_SECRET", "t".repeat(32)); // a rotated secret logs everyone out
    expect(await verifySessionToken(token, now)).toBeNull();
    expect(await verifySessionToken(undefined)).toBeNull();
    expect(await verifySessionToken("not-a-jwt")).toBeNull();
  });

  it("refuses to work with a missing or short secret", async () => {
    configure({ SESSION_SECRET: "short" });
    await expect(createSessionToken()).rejects.toBeInstanceOf(ConfigError);
    await expect(verifySessionToken("a.b.c")).rejects.toBeInstanceOf(ConfigError);
  });
});

describe("access code", () => {
  it("matches only the exact code", () => {
    configure();
    expect(accessCodeMatches(CODE)).toBe(true);
    expect(accessCodeMatches(CODE + " ")).toBe(false);
    expect(accessCodeMatches("")).toBe(false);
  });

  it("fails closed when no code is configured", () => {
    configure({ APP_ACCESS_CODE: "" });
    expect(() => accessCodeMatches("anything")).toThrow(ConfigError);
  });
});

describe("safeNextPath", () => {
  it.each([
    ["/briefs/abc?x=1", "/briefs/abc?x=1"],
    [null, "/"],
    ["", "/"],
    ["https://evil.example", "/"],
    ["//evil.example", "/"],
    ["/\\evil.example", "/"],
  ])("%s -> %s", (input, expected) => {
    expect(safeNextPath(input)).toBe(expected);
  });
});
