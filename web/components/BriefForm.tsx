"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { BriefSchema, DEFAULT_LOCATION, LEVELS } from "@/lib/schema";

// The brief request form. Validates with the same zod schema as the server (lib/schema.ts), then POSTs to
// /api/briefs and opens the brief's page, where progress streams in.

const LEVEL_LABELS: Record<(typeof LEVELS)[number], string> = { intern: "Intern", "new-grad": "New grad", experienced: "Experienced" };
const JD_MAX = 20_000;

type FieldErrors = Partial<Record<"company" | "role" | "level" | "location" | "jd", string>>;

export function BriefForm() {
  const router = useRouter();
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("new-grad");
  const [location, setLocation] = useState(DEFAULT_LOCATION);
  const [jd, setJd] = useState("");
  const [rerun, setRerun] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function clearError(field: keyof FieldErrors) {
    setErrors((prev) => (prev[field] ? { ...prev, [field]: undefined } : prev));
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const parsed = BriefSchema.safeParse({ company, role, level, location, jd, rerun });
    if (!parsed.success) {
      const next: FieldErrors = {};
      for (const issue of parsed.error.issues) {
        const field = issue.path[0] as keyof FieldErrors;
        next[field] ??= issue.message;
      }
      setErrors(next);
      return;
    }
    setErrors({});
    setBusy(true);
    try {
      const res = await fetch("/api/briefs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, role, level, location, jd, rerun }),
      });
      const body = (await res.json().catch(() => null)) as { job_id?: string; detail?: string } | null;
      if (res.status === 202 && body?.job_id) {
        router.push(`/briefs/${body.job_id}`);
        return;
      }
      if (res.status === 401) {
        router.replace("/login");
        return;
      }
      setFormError(
        res.status === 503
          ? "The brief service is waking up. Wait a few seconds and try again."
          : (body?.detail ?? `Something went wrong (${res.status}).`),
      );
    } catch {
      setFormError("Could not reach the server. Check your connection and try again.");
    }
    setBusy(false);
  }

  return (
    <form onSubmit={onSubmit} noValidate className="card space-y-5 p-5 sm:p-6">
      <div className="grid gap-5 sm:grid-cols-2">
        <Field id="company" label="Company" error={errors.company}>
          <input id="company" value={company} onChange={(e) => (setCompany(e.target.value), clearError("company"))} placeholder="e.g. Stripe" autoComplete="organization" className="input" aria-invalid={!!errors.company} />
        </Field>
        <Field id="role" label="Role" error={errors.role}>
          <input id="role" value={role} onChange={(e) => (setRole(e.target.value), clearError("role"))} placeholder="e.g. Software Engineer" autoComplete="organization-title" className="input" aria-invalid={!!errors.role} />
        </Field>
      </div>

      <div className="grid gap-5 sm:grid-cols-2">
        <fieldset>
          <legend className="label">Level</legend>
          <div className="mt-1.5 grid grid-cols-3 rounded-lg border border-zinc-300 p-0.5 dark:border-zinc-700">
            {LEVELS.map((l) => (
              <label key={l} className={`cursor-pointer rounded-md px-2 py-1.5 text-center text-sm font-medium transition has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-zinc-400 ${level === l ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900" : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"}`}>
                <input type="radio" name="level" value={l} checked={level === l} onChange={() => setLevel(l)} className="sr-only" />
                {LEVEL_LABELS[l]}
              </label>
            ))}
          </div>
        </fieldset>
        <Field id="location" label="Location" error={errors.location}>
          <input id="location" value={location} onChange={(e) => (setLocation(e.target.value), clearError("location"))} placeholder={DEFAULT_LOCATION} autoComplete="country-name" className="input" aria-invalid={!!errors.location} />
        </Field>
      </div>

      <details className="group rounded-lg border border-zinc-200 dark:border-zinc-800" open={jd.length > 0 || undefined}>
        <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2.5 text-sm font-medium">
          <span>
            Job description <span className="font-normal text-zinc-500">(optional)</span>
          </span>
          <span className="text-zinc-400 transition group-open:rotate-180">⌄</span>
        </summary>
        <div className="border-t border-zinc-200 p-3 dark:border-zinc-800">
          <textarea id="jd" value={jd} onChange={(e) => (setJd(e.target.value), clearError("jd"))} rows={7} placeholder="Paste the posting for facts about this exact role." className="input resize-y font-mono text-xs" aria-label="Job description" aria-invalid={!!errors.jd} />
          <div className="mt-1 flex justify-between text-xs text-zinc-500">
            <span className="text-red-600 dark:text-red-400">{errors.jd}</span>
            <span className={jd.length > JD_MAX ? "text-red-600" : ""}>
              {jd.length.toLocaleString()} / {JD_MAX.toLocaleString()}
            </span>
          </div>
        </div>
      </details>

      <label className="flex items-start gap-2.5 text-sm">
        <input type="checkbox" checked={rerun} onChange={(e) => setRerun(e.target.checked)} className="mt-0.5 h-4 w-4 accent-zinc-900 dark:accent-zinc-100" />
        <span>
          Build a fresh brief
          <span className="block text-zinc-500">Otherwise a brief with the same inputs from the last 7 days opens instantly.</span>
        </span>
      </label>

      {formError && (
        <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300">
          {formError}
        </p>
      )}

      <button type="submit" disabled={busy} className="btn-primary w-full py-2.5 sm:w-auto">
        {busy ? "Starting…" : "Build brief"}
      </button>
    </form>
  );
}

function Field({ id, label, error, children }: { id: string; label: string; error?: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="label">
        {label}
      </label>
      <div className="mt-1.5">{children}</div>
      {error && (
        <p id={`${id}-error`} className="mt-1 text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}
