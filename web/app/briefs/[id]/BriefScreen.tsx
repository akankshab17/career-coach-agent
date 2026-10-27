"use client";

import { Progress } from "@/components/Progress";
import { useBriefProgress } from "@/components/useBriefProgress";

export function BriefScreen({ id }: { id: string }) {
  const state = useBriefProgress(id);
  const job = state.job;

  return (
    <div className="space-y-6">
      {job && (
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {job.company} <span className="text-zinc-400">·</span> {job.role}
          </h1>
          <p className="mt-1 text-sm text-zinc-500">
            {job.level} · {job.location}
          </p>
        </div>
      )}
      {state.phase === "done" && job?.brief_markdown ? (
        <pre className="card overflow-x-auto whitespace-pre-wrap p-5 text-sm">{job.brief_markdown}</pre>
      ) : (
        <Progress state={state} />
      )}
    </div>
  );
}
