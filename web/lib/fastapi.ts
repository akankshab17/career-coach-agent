import "server-only";

// The only module that talks to the Python API. It runs on the server (route handlers), never in the browser, so the
// internal key stays secret. FastAPI rejects requests without the key when INTERNAL_API_KEY is set there (src/api.py).

export const INTERNAL_HEADER = "X-Internal-Key";
const DEFAULT_URL = "http://localhost:8000";
const DEFAULT_TIMEOUT_MS = 20_000; // Cloud Run cold starts take a while; the SSE route retries on top of this

/** FastAPI could not be reached (down, waking up, or too slow). Route handlers turn this into a 503. */
export class FastapiUnavailableError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "FastapiUnavailableError";
  }
}

export type FastapiResponse = { status: number; body: unknown };

export function fastapiUrl(): string {
  return (process.env.FASTAPI_URL || DEFAULT_URL).replace(/\/+$/, "");
}

/** Calls FastAPI with the internal key and returns its status and JSON body (null if the body isn't JSON). */
export async function callFastapi(
  path: string,
  init: { method?: "GET" | "POST"; json?: unknown; timeoutMs?: number } = {},
): Promise<FastapiResponse> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const key = process.env.INTERNAL_API_KEY;
  if (key) headers[INTERNAL_HEADER] = key;
  if (init.json !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(`${fastapiUrl()}${path}`, {
      method: init.method ?? "GET",
      headers,
      body: init.json === undefined ? undefined : JSON.stringify(init.json),
      signal: AbortSignal.timeout(init.timeoutMs ?? DEFAULT_TIMEOUT_MS),
      cache: "no-store",
    });
  } catch (err) {
    throw new FastapiUnavailableError("The brief service is not responding (it may be starting up).", { cause: err });
  }
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  return { status: res.status, body };
}
