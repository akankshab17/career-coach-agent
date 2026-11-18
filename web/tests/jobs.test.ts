import { describe, expect, it } from "vitest";
import { describeProgress, isJobId, type JobView } from "@/lib/jobs";
import { job } from "./helpers";

const view = (o: Record<string, unknown>) => describeProgress(job(o) as JobView);
const states = (o: Record<string, unknown>) => view(o).phases.map((p) => p.state[0]).join("");

describe("describeProgress", () => {
  it("maps pipeline steps to phases", () => {
    expect(states({ status: "queued", step: null })).toBe("pppppp");
    expect(states({ step: null })).toBe("appppp"); // running, no step reported yet
    expect(states({ step: "search" })).toBe("appppp");
    expect(states({ step: "extract" })).toBe("ddappp");
    expect(states({ step: "coverage" })).toBe("dddapp");
    expect(states({ step: "finalize" })).toBe("ddddda");
    expect(states({ status: "done", step: "finalize" })).toBe("dddddd");
  });

  it("flags the follow-up round while phases 1-4 repeat", () => {
    expect(view({ step: "search_more" }).followUp).toBe(true);
    expect(states({ step: "search_more" })).toBe("appppp");
    const second = view({ step: "fetch", steps: ["[coverage] 1 interview -> search more", "[search_more] new sources: 3"] });
    expect(second.followUp).toBe(true);
    expect(view({ step: "write", steps: ["[search_more] new sources: 3"] }).followUp).toBe(false);
  });

  it("shows the latest log line", () => {
    expect(view({ steps: ["[search] 12 sources", "[fetch] 8 pages fetched"] }).lastLine).toBe("[fetch] 8 pages fetched");
    expect(view({}).lastLine).toBeNull();
  });
});

describe("isJobId", () => {
  it("accepts FastAPI's 32-hex ids only", () => {
    expect(isJobId("0123456789abcdef0123456789abcdef")).toBe(true);
    expect(isJobId("../health")).toBe(false);
    expect(isJobId("0123456789ABCDEF0123456789ABCDEF")).toBe(false);
  });
});
