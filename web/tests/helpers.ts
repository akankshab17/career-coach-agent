import { NextRequest } from "next/server";
import { vi } from "vitest";

export const SECRET = "s".repeat(32);
export const CODE = "correct-horse-battery-staple";

/** The env a configured server has. Vitest's unstubEnvs undoes it after each test. */
export function configure(env: Record<string, string> = {}) {
  vi.stubEnv("SESSION_SECRET", SECRET);
  vi.stubEnv("APP_ACCESS_CODE", CODE);
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v);
}

export function request(path: string, init: { method?: string; json?: unknown; cookie?: string; ip?: string } = {}) {
  const headers = new Headers();
  if (init.json !== undefined) headers.set("Content-Type", "application/json");
  if (init.cookie) headers.set("Cookie", init.cookie);
  if (init.ip) headers.set("X-Forwarded-For", init.ip);
  return new NextRequest(new URL(path, "http://localhost:3000"), {
    method: init.method ?? "GET",
    headers,
    body: init.json === undefined ? undefined : JSON.stringify(init.json),
  });
}

/** "name=value" of the cookie a response sets, for sending it back on the next request. */
export function cookieFrom(response: Response): string {
  return (response.headers.get("Set-Cookie") ?? "").split(";")[0];
}
