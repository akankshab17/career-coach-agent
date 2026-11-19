import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { briefFileName, citationNumber, hostname, parseBrief, safeUrl } from "@/lib/brief";

// A real brief produced by the pipeline (committed in the repo).
const AMAZON = readFileSync(new URL("../../examples/amazon-sde-new-grad.md", import.meta.url), "utf8");

const SMALL = `# Interview brief: Acme — SWE (new grad)

## TL;DR
- Fact one. [1](https://acme.com/about)[2](https://forum.example/p/1)
- From your JD. [3]

## Sources
1. [About \\[Acme\\]](https://acme.com/about)
2. [Forum post](https://forum.example/p/1) (2026-10-15)
3. Job description provided by you
4. [Bad link](javascript:alert(1))
5. [Foo](https://en.wikipedia.org/wiki/Foo_(bar))

_Grounding: 5 facts grounded from 3 sources (1 rejected). Writing: 4 sentences checked, 0 dropped._
`;

describe("parseBrief", () => {
  it("splits title, body, sources and the grounding note", () => {
    const { body, sources, note } = parseBrief(SMALL);
    expect(body.startsWith("## TL;DR")).toBe(true);
    expect(body).not.toContain("## Sources");
    expect(body).not.toContain("Grounding");
    expect(note).toBe("Grounding: 5 facts grounded from 3 sources (1 rejected). Writing: 4 sentences checked, 0 dropped.");
    expect(sources).toEqual([
      { n: 1, title: "About [Acme]", url: "https://acme.com/about", date: null },
      { n: 2, title: "Forum post", url: "https://forum.example/p/1", date: "2026-10-15" },
      { n: 3, title: "Job description provided by you", url: null, date: null },
      { n: 4, title: "Bad link", url: null, date: null },
      { n: 5, title: "Foo", url: "https://en.wikipedia.org/wiki/Foo_(bar)", date: null },
    ]);
  });

  it("turns a bare JD citation into a link to its source entry, leaving real links alone", () => {
    const { body } = parseBrief(SMALL);
    expect(body).toContain("From your JD. [3](#source-3)");
    expect(body).toContain("[1](https://acme.com/about)\u2060[2](https://forum.example/p/1)");
  });

  it("parses a real pipeline brief", () => {
    const { body, sources, note } = parseBrief(AMAZON);
    expect(sources.map((s) => s.n)).toEqual(Array.from({ length: sources.length }, (_, i) => i + 1));
    expect(sources.length).toBeGreaterThanOrEqual(10);
    expect(sources.every((s) => s.url?.startsWith("https://"))).toBe(true);
    expect(note).toMatch(/^Grounding: \d+ facts grounded/);
    for (const heading of ["## TL;DR", "## 1. Company overview", "## 5. Questions to ask"]) expect(body).toContain(heading);
    // every citation in the body points at a listed source
    expect(body).toMatch(/\)\u2060\[\d+\]\(/); // merged facts' citations stay together
    const cited = [...body.matchAll(/\[(\d+)\]\(/g)].map((m) => Number(m[1]));
    expect(cited.length).toBeGreaterThan(10);
    expect(cited.every((n) => sources.some((s) => s.n === n))).toBe(true);
  });

  it("keeps one sentence per paragraph but leaves lists and headings alone", () => {
    const { body } = parseBrief("## 1. Company\nFirst. [1](https://a.com)\nSecond. [2](https://b.com)\n\n## 2. Prep\n- One\n- Two\n1. Three\n2. Four");
    expect(body).toBe("## 1. Company\nFirst. [1](https://a.com)\n\nSecond. [2](https://b.com)\n\n## 2. Prep\n- One\n- Two\n1. Three\n2. Four");
  });

  it("copes with a brief without sources or note", () => {
    expect(parseBrief("# T\n\n## TL;DR\n- Nothing found.")).toEqual({ body: "## TL;DR\n- Nothing found.", sources: [], note: null });
  });
});

describe("helpers", () => {
  it("safeUrl allows only http(s)", () => {
    expect(safeUrl("https://a.com/x")).toBe("https://a.com/x");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl("data:text/html,hi")).toBeNull();
    expect(safeUrl("not a url")).toBeNull();
  });

  it("citationNumber recognises numeric link text", () => {
    expect(citationNumber("12")).toBe(12);
    expect(citationNumber("Forum post")).toBeNull();
  });

  it("hostname drops www", () => {
    expect(hostname("https://www.reddit.com/r/x")).toBe("reddit.com");
  });

  it("briefFileName is company-role-level.md", () => {
    expect(briefFileName({ company: "Amazon", role: "Software Development Engineer", level: "new-grad" })).toBe(
      "amazon-software-development-engineer-new-grad.md",
    );
    expect(briefFileName({ company: "AT&T", role: "SWE / Backend", level: "intern" })).toBe("at-t-swe-backend-intern.md");
  });
});
