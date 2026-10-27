"use client";

import Link from "next/link";
import { BriefView } from "@/components/BriefView";
import { Progress } from "@/components/Progress";
import { useBriefProgress } from "@/components/useBriefProgress";
import { briefFileName } from "@/lib/brief";
import type { JobView } from "@/lib/jobs";

const LEVEL_LABELS: Record<string, string> = { intern: "Intern", "new-grad": "New grad", experienced: "Experienced" };

export function BriefScreen({ id }: { id: string }) {
  const state = useBriefProgress(id);
  const job = state.job;
  const done = state.phase === "done" && job?.brief_markdown;

  return (
    <div className="space-y-6">
      {job && (
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold tracking-tight break-words">
              {job.company} <span className="font-normal text-zinc-400">·</span> {job.role}
            </h1>
            <p className="mt-1 text-sm text-zinc-500">
              {LEVEL_LABELS[job.level] ?? job.level} · {job.location}
              {job.has_jd && " · with job description"}
              {done && job.finished_at && ` · built ${formatDate(job.finished_at)}`}
            </p>
          </div>
          {done && (
            <div className="flex shrink-0 gap-2">
              <button onClick={() => download(job)} className="btn-secondary">
                Download .md
              </button>
              <Link href="/" className="btn-primary">
                New brief
              </Link>
            </div>
          )}
        </div>
      )}
      {done ? <BriefView markdown={job.brief_markdown!} /> : <Progress state={state} />}
    </div>
  );
}

function download(job: JobView) {
  const url = URL.createObjectURL(new Blob([job.brief_markdown ?? ""], { type: "text/markdown;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: briefFileName(job) });
  a.click();
  URL.revokeObjectURL(url);
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
