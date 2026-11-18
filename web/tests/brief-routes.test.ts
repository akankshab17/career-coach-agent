import { describe, expect, it, vi } from "vitest";
import { GET as getBrief } from "@/app/api/briefs/[id]/route";
import { GET as events } from "@/app/api/briefs/[id]/events/route";
import { POST as login } from "@/app/api/login/route";
import { CODE, configure, cookieFrom, ctx, job, JOB_ID, request } from "./helpers";

async function loggedIn() {
  configure({ INTERNAL_API_KEY: "k".repeat(40), FASTAPI_URL: "http://api.test" });
  return cookieFrom(await login(request("/api/login", { method: "POST", json: { code: CODE } })));
}

/** FastAPI answering GET /briefs/{id} with these (status, body) pairs in turn. */
function fastapi(...answers: [number, unknown][]) {
  let i = 0;
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => {
    const [status, body] = answers[Math.min(i++, answers.length - 1)];
    return Response.json(body, { status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("GET /api/briefs/:id", () => {
  it("returns the job from FastAPI", async () => {
    const cookie = await loggedIn();
    const fetchMock = fastapi([200, job({ status: "done", brief_markdown: "# B" })]);
    const res = await getBrief(request(`/api/briefs/${JOB_ID}`, { cookie }), ctx(JOB_ID));
    expect(res.status).toBe(200);
    expect((await res.json()).brief_markdown).toBe("# B");
    expect(fetchMock.mock.calls[0][0]).toBe(`http://api.test/briefs/${JOB_ID}`);
  });

  it("404 (expired) for an unknown job, and for a malformed id without calling FastAPI", async () => {
    const cookie = await loggedIn();
    const fetchMock = fastapi([404, { detail: "No such brief." }]);
    expect((await getBrief(request(`/api/briefs/${JOB_ID}`, { cookie }), ctx(JOB_ID))).status).toBe(404);
    expect((await getBrief(request("/api/briefs/x", { cookie }), ctx("..%2Fhealth"))).status).toBe(404);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("needs a session", async () => {
    configure();
    expect((await getBrief(request(`/api/briefs/${JOB_ID}`), ctx(JOB_ID))).status).toBe(401);
  });
});

describe("GET /api/briefs/:id/events", () => {
  it("streams progress as SSE until the brief is done", async () => {
    vi.useFakeTimers();
    const cookie = await loggedIn();
    fastapi([200, job({ status: "queued", step: null })], [200, job({ step: "write" })], [200, job({ status: "done", brief_markdown: "# B" })]);

    const res = await events(request(`/api/briefs/${JOB_ID}/events`, { cookie }), ctx(JOB_ID));
    expect(res.headers.get("Content-Type")).toBe("text/event-stream; charset=utf-8");
    const reading = res.text();
    await vi.runAllTimersAsync();
    const text = await reading;
    vi.useRealTimers();

    expect(text.startsWith("retry: 2000\n\n")).toBe(true);
    const names = [...text.matchAll(/^event: (\w+)$/gm)].map((m) => m[1]);
    expect(names).toEqual(["job", "job", "job"]);
    expect(text).toContain('"brief_markdown":"# B"');
  });

  it("sends expired and closes when FastAPI no longer has the job", async () => {
    const cookie = await loggedIn();
    fastapi([404, { detail: "No such brief." }]);
    const text = await (await events(request(`/api/briefs/${JOB_ID}/events`, { cookie }), ctx(JOB_ID))).text();
    expect(text).toBe("retry: 2000\n\nevent: expired\ndata: {}\n\n");
  });

  it("refuses without a session or with a malformed id", async () => {
    configure();
    expect((await events(request(`/api/briefs/${JOB_ID}/events`), ctx(JOB_ID))).status).toBe(401);
    const cookie = await loggedIn();
    expect((await events(request("/api/briefs/nope/events", { cookie }), ctx("nope"))).status).toBe(404);
  });
});

describe("events stream cancellation", () => {
  it("stops polling and doesn't throw when the browser disconnects mid-poll", async () => {
    const cookie = await loggedIn();
    const fetchMock = fastapi([200, job({ step: "write" })]);
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const res = await events(request(`/api/briefs/${JOB_ID}/events`, { cookie }), ctx(JOB_ID));
    const reader = res.body!.getReader();
    await reader.read(); // retry line
    const pending = reader.read(); // first poll starts
    await reader.cancel();
    await pending.catch(() => {});
    await new Promise((r) => setTimeout(r, 20));
    expect(errors).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(2);
  });
});
