import { describe, expect, it } from "vitest";
import { POST as login } from "@/app/api/login/route";
import { POST as logout } from "@/app/api/logout/route";
import { proxy } from "@/proxy";
import { CODE, configure, cookieFrom, request } from "./helpers";

describe("POST /api/login", () => {
  it("sets an httpOnly session cookie for the right code", async () => {
    configure();
    const res = await login(request("/api/login", { method: "POST", json: { code: CODE } }));
    expect(res.status).toBe(200);
    const setCookie = res.headers.get("Set-Cookie") ?? "";
    expect(setCookie).toMatch(/^ib_session=[\w-]+\.[\w-]+\.[\w-]+;/);
    expect(setCookie).toMatch(/HttpOnly/i);
    expect(setCookie).toMatch(/SameSite=lax/i);
    expect(setCookie).toMatch(/Max-Age=604800/);
  });

  it("rejects a wrong code without a cookie", async () => {
    configure();
    const res = await login(request("/api/login", { method: "POST", json: { code: "nope" } }));
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: "Wrong access code." });
    expect(res.headers.get("Set-Cookie")).toBeNull();
  });

  it.each([[{}], [{ code: "" }], [{ code: 42 }], [null]])("rejects a malformed body %j with 400", async (json) => {
    configure();
    const res = await login(request("/api/login", { method: "POST", json }));
    expect(res.status).toBe(400);
  });

  it("fails closed (500) when the server has no access code or secret", async () => {
    configure({ APP_ACCESS_CODE: "" });
    expect((await login(request("/api/login", { method: "POST", json: { code: "x" } }))).status).toBe(500);
    configure({ SESSION_SECRET: "" });
    expect((await login(request("/api/login", { method: "POST", json: { code: CODE } }))).status).toBe(500);
  });
});

describe("proxy", () => {
  async function loggedInCookie() {
    return cookieFrom(await login(request("/api/login", { method: "POST", json: { code: CODE } })));
  }

  it("redirects pages to /login, keeping where the visitor was going", async () => {
    configure();
    const res = await proxy(request("/briefs/abc?tab=sources"));
    expect(res.status).toBe(307);
    expect(res.headers.get("Location")).toBe("http://localhost:3000/login?next=%2Fbriefs%2Fabc%3Ftab%3Dsources");
    expect((await proxy(request("/"))).headers.get("Location")).toBe("http://localhost:3000/login");
  });

  it("answers 401 on API routes without a session", async () => {
    configure();
    const res = await proxy(request("/api/briefs", { method: "POST" }));
    expect(res.status).toBe(401);
  });

  it("lets a valid session through", async () => {
    configure();
    const res = await proxy(request("/", { cookie: await loggedInCookie() }));
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("treats a bad cookie or a misconfigured server as logged out", async () => {
    configure();
    expect((await proxy(request("/", { cookie: "ib_session=garbage" }))).status).toBe(307);
    const cookie = await loggedInCookie();
    configure({ SESSION_SECRET: "" });
    expect((await proxy(request("/", { cookie }))).status).toBe(307);
  });

  it("logout clears the cookie", async () => {
    const res = await logout();
    expect(res.headers.get("Set-Cookie")).toMatch(/^ib_session=;.*Max-Age=0/i);
  });
});
