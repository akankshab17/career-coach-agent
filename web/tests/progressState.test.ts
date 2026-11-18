import { describe, expect, it } from "vitest";
import type { JobView } from "@/lib/jobs";
import { initialProgress, isTerminal, progressReducer, type ProgressAction } from "@/lib/progressState";
import { job } from "./helpers";

const reduce = (...actions: ProgressAction[]) => actions.reduce(progressReducer, initialProgress);
const j = (o: Record<string, unknown>) => job(o) as JobView;

describe("progressReducer", () => {
  it("follows the job's status", () => {
    expect(reduce({ type: "job", job: j({ status: "queued" }) }).phase).toBe("queued");
    expect(reduce({ type: "job", job: j({ status: "running" }) }).phase).toBe("running");
    const done = reduce({ type: "job", job: j({ status: "done", brief_markdown: "# B" }) });
    expect(done.phase).toBe("done");
    expect(done.job?.brief_markdown).toBe("# B");
  });

  it("shows waking but keeps the known job, and recovers", () => {
    const s = reduce({ type: "job", job: j({ step: "extract" }) }, { type: "waking" });
    expect(s.phase).toBe("waking");
    expect(s.job?.step).toBe("extract");
    expect(progressReducer(s, { type: "job", job: j({ step: "ground" }) }).phase).toBe("running");
  });

  it("never leaves a finished state for waking", () => {
    expect(reduce({ type: "job", job: j({ status: "done" }) }, { type: "waking" }).phase).toBe("done");
    expect(reduce({ type: "expired" }, { type: "waking" }).phase).toBe("expired");
  });

  it("records expired and error details", () => {
    expect(reduce({ type: "expired" })).toEqual({ phase: "expired", job: null, detail: null });
    expect(reduce({ type: "error", detail: "misconfigured" })).toMatchObject({ phase: "error", detail: "misconfigured" });
  });

  it("knows which phases are terminal", () => {
    expect(["done", "failed", "expired", "error"].every((p) => isTerminal(p as never))).toBe(true);
    expect(["connecting", "waking", "queued", "running"].some((p) => isTerminal(p as never))).toBe(false);
  });
});
