import { describe, expect, it, vi } from "vitest";
import { POST as createBrief } from "@/app/api/briefs/route";
import { POST as login } from "@/app/api/login/route";
import { CODE, configure, cookieFrom, request } from "./helpers";

const BODY = { company: "Stripe", role: "Software Engineer" };
const JOB = { job_id: "j1", status: "queued", step: null, steps: [], company: "Stripe", reused: false };

async function loggedIn(ip = "203.0.113.1") {
  configure({ INTERNAL_API_KEY: "k".repeat(40), FASTAPI_URL: "http://api.test" });
  return cookieFrom(await login(request("/api/login", { method: "POST", json: { code: CODE }, ip })));
}

function fastapi(status: number, body: unknown) {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => Response.json(body, { status }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const post = (cookie: string, json: unknown = BODY, ip = "203.0.113.1") =>
  createBrief(request("/api/briefs", { method: "POST", json, cookie, ip }));

describe("POST /api/briefs", () => {
  it("validates, then forwards to FastAPI with the internal key and returns the job", async () => {
    const cookie = await loggedIn();
    const fetchMock = fastapi(202, JOB);

    const res = await post(cookie, { ...BODY, level: "intern", rerun: true });

    expect(res.status).toBe(202);
    expect(await res.json()).toEqual(JOB);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/briefs");
    expect((init.headers as Record<string, string>)["X-Internal-Key"]).toBe("k".repeat(40));
    expect(JSON.parse(init.body as string)).toEqual({
      company: "Stripe", role: "Software Engineer", level: "intern", location: "United States", jd: null, force: true,
    });
  });

  it("needs a session", async () => {
    configure();
    const fetchMock = fastapi(202, JOB);
    expect((await post("")).status).toBe(401);
    expect((await post("ib_session=forged")).status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects invalid input with 400 before calling FastAPI", async () => {
    const cookie = await loggedIn();
    const fetchMock = fastapi(202, JOB);
    const res = await post(cookie, { company: "", role: "SWE" });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ detail: "Company is required." });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("limits each session to 3 briefs per 10 minutes, with Retry-After", async () => {
    const cookie = await loggedIn();
    const fetchMock = fastapi(202, JOB);
    for (let i = 0; i < 3; i++) expect((await post(cookie)).status).toBe(202);
    const res = await post(cookie);
    expect(res.status).toBe(429);
    expect(Number(res.headers.get("Retry-After"))).toBeGreaterThan(500);
    expect((await res.json()).detail).toMatch(/Try again in 10 minutes/);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("also limits per IP, so logging in again doesn't reset the count", async () => {
    fastapi(202, JOB);
    let accepted = 0;
    for (let session = 0; session < 5; session++) {
      const cookie = await loggedIn();
      for (let i = 0; i < 3; i++) if ((await post(cookie)).status === 202) accepted++;
    }
    expect(accepted).toBe(10);
  });

  it("invalid requests don't use up the limit", async () => {
    const cookie = await loggedIn();
    fastapi(202, JOB);
    for (let i = 0; i < 5; i++) await post(cookie, { company: "" });
    expect((await post(cookie)).status).toBe(202);
  });

  it.each([
    [429, { detail: "Daily limit reached: 5 new briefs ..." }, 429, "Daily limit reached: 5 new briefs ..."],
    [401, { detail: "Missing or wrong internal API key." }, 502, "The brief service is misconfigured."],
    [422, { detail: [{ msg: "x" }] }, 400, "The brief service rejected these inputs."],
    [500, null, 502, "The brief service had an error."],
  ])("maps FastAPI %i to %i", async (upstream, body, expected, detail) => {
    const cookie = await loggedIn();
    fastapi(upstream, body);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const res = await post(cookie);
    expect(res.status).toBe(expected);
    expect(await res.json()).toEqual({ detail });
  });

  it("answers 503 with Retry-After when FastAPI is unreachable", async () => {
    const cookie = await loggedIn();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("fetch failed"); }));
    const res = await post(cookie);
    expect(res.status).toBe(503);
    expect(res.headers.get("Retry-After")).toBe("10");
  });
});

describe("login rate limit", () => {
  it("allows 10 attempts per IP per 15 minutes, right or wrong", async () => {
    configure();
    const attempt = (code: string, ip: string) => login(request("/api/login", { method: "POST", json: { code }, ip }));
    for (let i = 0; i < 10; i++) expect((await attempt("wrong", "198.51.100.9")).status).toBe(401);
    const blocked = await attempt(CODE, "198.51.100.9");
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get("Retry-After")).toBeTruthy();
    expect((await attempt(CODE, "198.51.100.10")).status).toBe(200); // another IP is unaffected
  });
});
