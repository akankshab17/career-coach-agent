import { describe, expect, it, vi } from "vitest";
import { FastapiUnavailableError, type FastapiResponse } from "@/lib/fastapi";
import { briefEvents, formatSse, type BriefEvent } from "@/lib/progress";
import { job } from "./helpers";

type Answer = FastapiResponse | "down";

/** Runs briefEvents over scripted FastAPI answers with a fake clock (each sleep advances it). */
async function run(answers: Answer[], opts: { maxMs?: number; intervalMs?: number } = {}) {
  let t = 0;
  let i = 0;
  const fetchJob = vi.fn(async () => {
    const a = answers[Math.min(i++, answers.length - 1)];
    if (a === "down") throw new FastapiUnavailableError("down");
    return a;
  });
  const events: BriefEvent[] = [];
  for await (const e of briefEvents({ fetchJob, sleep: async (ms) => void (t += ms), now: () => t, ...opts })) events.push(e);
  return { events, polls: fetchJob.mock.calls.length, elapsed: t };
}

const ok = (overrides: Record<string, unknown>): FastapiResponse => ({ status: 200, body: job(overrides) });

describe("briefEvents", () => {
  it("emits the job only when it changes, pings otherwise, and stops when it is done", async () => {
    const { events } = await run([
      ok({ status: "queued", step: null }),
      ok({ status: "queued", step: null }),
      ok({ step: "search" }),
      ok({ step: "search", steps: ["[search] 12 sources"] }),
      ok({ status: "done", step: "finalize", brief_markdown: "# Brief" }),
      ok({ status: "done" }), // never polled
    ]);
    expect(events.map((e) => (e.type === "job" ? `${e.job.status}:${e.job.step}:${e.job.steps.length}` : e.type))).toEqual([
      "queued:null:0", "ping", "running:search:0", "running:search:1", "done:finalize:0",
    ]);
  });

  it("stops after a failed job", async () => {
    const { events, polls } = await run([ok({ status: "failed", error: "Groq daily limit" }), ok({})]);
    expect(events).toHaveLength(1);
    expect(polls).toBe(1);
  });

  it("reports waking while FastAPI is unreachable, then carries on", async () => {
    const { events } = await run(["down", "down", ok({ status: "done" })]);
    expect(events.map((e) => e.type)).toEqual(["waking", "waking", "job"]);
  });

  it("ends with expired on 404 and fatal on 401", async () => {
    expect((await run([{ status: 404, body: { detail: "No such brief." } }])).events).toEqual([{ type: "expired" }]);
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect((await run([{ status: 401, body: null }])).events).toEqual([{ type: "fatal", detail: "The brief service is misconfigured." }]);
  });

  it("tolerates up to two 5xx in a row, reports the third", async () => {
    const blip: FastapiResponse = { status: 502, body: null };
    expect((await run([blip, blip, ok({ status: "done" })])).events.map((e) => e.type)).toEqual(["job"]);
    expect((await run([blip, blip, blip])).events).toEqual([{ type: "fatal", detail: "The brief service had an error (502)." }]);
  });

  it("ends the stream before maxMs so the browser reconnects", async () => {
    const { polls, elapsed } = await run([ok({})], { maxMs: 50_000, intervalMs: 2_000 });
    expect(polls).toBe(25);
    expect(elapsed).toBeLessThan(50_000);
  });

  it("stops polling when the browser disconnects", async () => {
    const controller = new AbortController();
    const fetchJob = vi.fn(async () => ok({}));
    const seen: BriefEvent[] = [];
    for await (const e of briefEvents({ fetchJob, sleep: async () => controller.abort(), signal: controller.signal })) seen.push(e);
    expect(fetchJob).toHaveBeenCalledTimes(1);
  });
});

describe("formatSse", () => {
  it("writes named events with JSON data, and pings as comments", () => {
    expect(formatSse({ type: "expired" })).toBe("event: expired\ndata: {}\n\n");
    expect(formatSse({ type: "fatal", detail: "x" })).toBe('event: fatal\ndata: {"detail":"x"}\n\n');
    expect(formatSse({ type: "ping" })).toBe(": ping\n\n");
  });
});
