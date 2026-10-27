"use client";

import { useState, type FormEvent } from "react";
import { safeNextPath } from "@/lib/paths";

// Reads ?next= from window.location at submit time rather than useSearchParams, so the page itself stays static.
function nextPath(): string {
  return safeNextPath(new URLSearchParams(window.location.search).get("next"));
}

export function LoginForm() {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (res.ok) {
        window.location.assign(nextPath()); // full load, so the proxy sees the new cookie
        return;
      }
      const body = (await res.json().catch(() => null)) as { detail?: string } | null;
      setError(body?.detail ?? `Login failed (${res.status}).`);
    } catch {
      setError("Could not reach the server.");
    }
    setBusy(false);
  }

  return (
    <form onSubmit={onSubmit} className="mt-6 flex flex-col gap-3">
      <label htmlFor="code" className="text-sm font-medium">
        Access code
      </label>
      <input
        id="code"
        type="password"
        autoComplete="current-password"
        value={code}
        onChange={(e) => setCode(e.target.value)}
        className="rounded-md border border-zinc-300 bg-transparent px-3 py-2 dark:border-zinc-700"
        required
      />
      {error && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="rounded-md bg-zinc-900 px-3 py-2 font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
      >
        {busy ? "Checking…" : "Continue"}
      </button>
    </form>
  );
}
