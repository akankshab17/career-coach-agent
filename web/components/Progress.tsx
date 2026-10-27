"use client";

import Link from "next/link";
import { useState } from "react";
import { describeProgress, type PhaseState } from "@/lib/jobs";
import type { ProgressState } from "@/lib/progressState";
import { formatElapsed, useNow } from "./useNow";

// Everything shown before a brief is ready (and when it can't be): live steps, waiting, waking, expired, errors.

export function Progress({ state, onRebuild }: { state: ProgressState; onRebuild?: () => void }) {
  const { phase, job, detail } = state;
  const [wakingSince] = useState(() => Date.now());
  const now = useNow(phase === "running" || phase === "waking" || phase === "queued");

  if (phase === "connecting") return <Notice tone="neutral" title="Connecting…" spinner />;

  if (phase === "waking") {
    return (
      <Notice tone="amber" title="Waking up the brief service…" spinner>
        The server sleeps when nobody is using it, so the first request can take up to a minute.{" "}
        <span className="tabular-nums">({formatElapsed((now - wakingSince) / 1000)})</span>
      </Notice>
    );
  }

  if (phase === "expired") {
    return (
      <Notice tone="neutral" title="This brief has expired">
        The server no longer has it: its storage is cleared when it restarts after a period of inactivity.
        <div className="mt-4 flex flex-wrap gap-3">
          {onRebuild && (
            <button onClick={onRebuild} className="btn-primary">
              Build it again
            </button>
          )}
          <Link href="/" className="btn-secondary">
            New brief
          </Link>
        </div>
      </Notice>
    );
  }

  if (phase === "error") {
    return (
      <Notice tone="red" title="Something went wrong">
        {detail}
        <div className="mt-4">
          <button onClick={() => window.location.reload()} className="btn-secondary">
            Try again
          </button>
        </div>
      </Notice>
    );
  }

  if (phase === "failed") {
    return (
      <Notice tone="red" title="The brief could not be built">
        {job?.error ?? "The pipeline stopped with an error."}
        <div className="mt-4 flex flex-wrap gap-3">
          {onRebuild && (
            <button onClick={onRebuild} className="btn-secondary">
              Try again
            </button>
          )}
        </div>
      </Notice>
    );
  }

  if (!job) return null;

  if (phase === "queued") {
    return (
      <Notice tone="neutral" title="Waiting in line" spinner>
        Another brief is being built; briefs run one at a time. Yours starts as soon as it finishes.
      </Notice>
    );
  }

  const progress = describeProgress(job);
  const started = job.started_at ? Date.parse(job.started_at) : null;
  return (
    <section aria-label="Progress" className="card p-5 sm:p-6">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="font-semibold">Building your brief</h2>
        {started !== null && phase === "running" && (
          <span className="text-sm tabular-nums text-zinc-500">{formatElapsed((now - started) / 1000)}</span>
        )}
      </div>
      <p className="mt-1 text-sm text-zinc-500">Usually 1 to 3 minutes. You can leave this page and come back.</p>
      <ol className="mt-5 space-y-3">
        {progress.phases.map((p) => (
          <li key={p.label} className="flex items-center gap-3">
            <StepIcon state={p.state} />
            <span className={p.state === "pending" ? "text-zinc-400 dark:text-zinc-500" : p.state === "active" ? "font-medium" : ""}>
              {p.label}
            </span>
          </li>
        ))}
      </ol>
      {progress.followUp && (
        <p className="mt-4 text-sm text-zinc-600 dark:text-zinc-400">
          Not enough interview data yet: running one follow-up search.
        </p>
      )}
      {progress.lastLine && (
        <p className="mt-4 truncate rounded-md bg-zinc-100 px-3 py-2 font-mono text-xs text-zinc-600 dark:bg-zinc-800/60 dark:text-zinc-400" title={progress.lastLine}>
          {progress.lastLine}
        </p>
      )}
    </section>
  );
}

function StepIcon({ state }: { state: PhaseState }) {
  if (state === "done") {
    return (
      <span aria-label="done" className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-600 text-white">
        <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2.5">
          <path d="M3.5 8.5l3 3 6-7" />
        </svg>
      </span>
    );
  }
  if (state === "active") return <Spinner label="in progress" />;
  return <span aria-label="pending" className="h-5 w-5 rounded-full border-2 border-zinc-300 dark:border-zinc-700" />;
}

function Spinner({ label }: { label: string }) {
  return (
    <span
      aria-label={label}
      className="h-5 w-5 shrink-0 animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-900 dark:border-zinc-700 dark:border-t-zinc-100"
    />
  );
}

const TONES = {
  neutral: "border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900",
  amber: "border-amber-300 bg-amber-50 dark:border-amber-800/60 dark:bg-amber-950/30",
  red: "border-red-300 bg-red-50 dark:border-red-900/60 dark:bg-red-950/30",
};

function Notice(props: { tone: keyof typeof TONES; title: string; spinner?: boolean; children?: React.ReactNode }) {
  return (
    <section role="status" className={`rounded-xl border p-5 sm:p-6 ${TONES[props.tone]}`}>
      <div className="flex items-center gap-3">
        {props.spinner && <Spinner label="working" />}
        <h2 className="font-semibold">{props.title}</h2>
      </div>
      {props.children && <div className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">{props.children}</div>}
    </section>
  );
}
