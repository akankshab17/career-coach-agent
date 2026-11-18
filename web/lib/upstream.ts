import "server-only";
import { jsonError } from "@/lib/http";

/** FastAPI's {detail} message, when its body has one. */
export function detailOf(body: unknown): string | null {
  return typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string" ? body.detail : null;
}

/** A FastAPI error status as the browser should see it: no setup details, and only statuses it can act on. */
export function upstreamError(status: number, body: unknown, route: string): Response {
  const detail = detailOf(body);
  if (status === 429) return jsonError(429, detail ?? "The daily limit has been reached."); // FastAPI's daily cap / full queue
  if (status === 404) return jsonError(404, "This brief is no longer stored on the server.");
  if (status === 422) return jsonError(400, "The brief service rejected these inputs."); // our schema should catch these first
  if (status === 401 || status === 403) {
    console.error(`[${route}] FastAPI answered ${status}: check INTERNAL_API_KEY on both servers`);
    return jsonError(502, "The brief service is misconfigured.");
  }
  console.error(`[${route}] FastAPI answered ${status}`);
  return jsonError(502, "The brief service had an error.");
}
