import { afterEach, describe, expect, it, vi } from "vitest";
import { callFastapi, FastapiUnavailableError, fastapiUrl } from "@/lib/fastapi";

function stubFetch(response: Response | Error) {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit): Promise<Response> => {
    if (response instanceof Error) throw response;
    return response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentHeaders(fetchMock: ReturnType<typeof stubFetch>): Record<string, string> {
  return fetchMock.mock.calls[0][1].headers as Record<string, string>;
}

afterEach(() => vi.unstubAllEnvs());

describe("callFastapi", () => {
  it("sends the internal key when it is set", async () => {
    vi.stubEnv("INTERNAL_API_KEY", "k".repeat(40));
    vi.stubEnv("FASTAPI_URL", "https://api.example.run.app/");
    const fetchMock = stubFetch(Response.json({ status: "ok" }));

    const res = await callFastapi("/health");

    expect(fetchMock.mock.calls[0][0]).toBe("https://api.example.run.app/health");
    expect(sentHeaders(fetchMock)["X-Internal-Key"]).toBe("k".repeat(40));
    expect(res).toEqual({ status: 200, body: { status: "ok" } });
  });

  it("sends no key header when none is configured (local development)", async () => {
    vi.stubEnv("INTERNAL_API_KEY", "");
    const fetchMock = stubFetch(Response.json({}));
    await callFastapi("/health");
    expect(sentHeaders(fetchMock)).not.toHaveProperty("X-Internal-Key");
  });

  it("posts JSON bodies", async () => {
    const fetchMock = stubFetch(Response.json({ job_id: "j1" }, { status: 202 }));
    const res = await callFastapi("/briefs", { method: "POST", json: { company: "Acme" } });
    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"company":"Acme"}');
    expect(sentHeaders(fetchMock)["Content-Type"]).toBe("application/json");
    expect(res.status).toBe(202);
  });

  it("passes error statuses through and tolerates non-JSON bodies", async () => {
    stubFetch(new Response("Internal Server Error", { status: 500 }));
    expect(await callFastapi("/briefs/x")).toEqual({ status: 500, body: null });
  });

  it("turns network errors and timeouts into FastapiUnavailableError", async () => {
    stubFetch(new TypeError("fetch failed"));
    await expect(callFastapi("/health")).rejects.toBeInstanceOf(FastapiUnavailableError);
    stubFetch(new DOMException("The operation timed out.", "TimeoutError"));
    await expect(callFastapi("/health")).rejects.toBeInstanceOf(FastapiUnavailableError);
  });

  it("defaults to the local API", () => {
    vi.stubEnv("FASTAPI_URL", "");
    expect(fastapiUrl()).toBe("http://localhost:8000");
  });
});
