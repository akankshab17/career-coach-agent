// The job as FastAPI reports it (src/api.py job_view), plus how its progress is shown. Safe to import in the browser.

export type JobStatus = "queued" | "running" | "done" | "failed";

export type JobView = {
  job_id: string;
  status: JobStatus;
  step: string | null; // the pipeline step running now (graph.py node name)
  steps: string[]; // log lines so far, e.g. "[search] 14 sources (...)"
  company: string;
  role: string;
  level: string;
  location: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  has_jd: boolean;
  reused: boolean;
  brief_markdown?: string; // when done
  debug_summary?: Record<string, unknown> | null;
};

const JOB_ID = /^[0-9a-f]{32}$/; // jobs.py: uuid4().hex

export function isJobId(id: string): boolean {
  return JOB_ID.test(id);
}

export function isFinished(job: Pick<JobView, "status">): boolean {
  return job.status === "done" || job.status === "failed";
}

// The pipeline's 8 steps grouped into what a reader cares about. A follow-up search (search_more) runs
// fetch -> extract -> ground -> coverage again, so phases 1-4 can repeat once.
export const PHASES = [
  { label: "Searching the web", steps: ["search", "search_more"] },
  { label: "Reading pages", steps: ["fetch"] },
  { label: "Extracting facts", steps: ["extract"] },
  { label: "Checking facts against sources", steps: ["ground", "coverage"] },
  { label: "Writing the brief", steps: ["write"] },
  { label: "Final checks and citations", steps: ["finalize"] },
] as const;

export type PhaseState = "done" | "active" | "pending";

export type ProgressView = {
  phases: { label: string; state: PhaseState }[];
  followUp: boolean; // in the follow-up search round
  lastLine: string | null;
};

export function describeProgress(job: Pick<JobView, "status" | "step" | "steps">): ProgressView {
  const followUp = job.steps.some((line) => line.startsWith("[search_more]")) || job.step === "search_more";
  const lastLine = job.steps.at(-1) ?? null;
  let current: number; // index of the active phase; -1 = not started, PHASES.length = all done
  if (job.status === "done") current = PHASES.length;
  else if (job.status === "queued") current = -1;
  else current = Math.max(0, PHASES.findIndex((p) => (p.steps as readonly string[]).includes(job.step ?? "")));
  const phases = PHASES.map((p, i) => ({
    label: p.label,
    state: (i < current ? "done" : i === current ? "active" : "pending") as PhaseState,
  }));
  return { phases, followUp: followUp && current >= 0 && current <= 3, lastLine };
}
