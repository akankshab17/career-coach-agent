import { callFastapi, FastapiUnavailableError } from "@/lib/fastapi";

// GET /api/health: is this server up, and can it reach FastAPI? Open (no login): it reveals nothing private.
export async function GET() {
  try {
    const { status } = await callFastapi("/health", { timeoutMs: 5_000 });
    return Response.json({ web: "ok", api: status === 200 ? "ok" : `error ${status}` });
  } catch (err) {
    if (err instanceof FastapiUnavailableError) return Response.json({ web: "ok", api: "unreachable" }, { status: 503 });
    throw err;
  }
}
