import { unstable_doesMiddlewareMatch } from "next/experimental/testing/server";
import { describe, expect, it } from "vitest";
import { config } from "@/proxy";

// Which paths proxy.ts runs on (i.e. which need a session).
describe("proxy matcher", () => {
  it.each([
    ["/", true],
    ["/briefs/abc", true],
    ["/api/briefs", true],
    ["/api/briefs/abc/events", true],
    ["/login", false],
    ["/api/login", false],
    ["/api/health", false],
    ["/_next/static/chunks/main.js", false],
    ["/_next/webpack-hmr", false], // redirecting this made /login reload in a loop in dev
    ["/favicon.ico", false],
  ])("%s -> %s", (url, expected) => {
    expect(unstable_doesMiddlewareMatch({ config, url })).toBe(expected);
  });
});
