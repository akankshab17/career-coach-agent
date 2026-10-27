import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BriefView } from "@/components/BriefView";

const BRIEF = `# Interview brief: Acme — SWE (new grad)

## TL;DR
- Candidates report two rounds. [1](https://forum.example/p/1)[2](https://acme.com/jobs/1)
- From your JD. [3]
- See [the docs](https://docs.example/x) and [a bad one](javascript:alert(1)).

<script>alert("x")</script>

## Sources
1. [Forum post](https://forum.example/p/1) (2026-10-15)
2. [Careers page](https://acme.com/jobs/1)
3. Job description provided by you

_Grounding: 3 facts grounded from 3 sources (0 rejected)._
`;

const html = renderToStaticMarkup(<BriefView markdown={BRIEF} />);

describe("BriefView", () => {
  it("renders citations as chips that jump to their source", () => {
    expect(html).toMatch(/<a href="#source-1" title="Forum post" aria-label="Source 1: Forum post" class="citation[^"]*">1<\/a>/);
    expect(html).toContain('href="#source-2"');
    expect(html).toContain('href="#source-3" title="Job description provided by you"');
    expect(html).not.toContain('href="https://forum.example/p/1" target'.replace(" target", '">1'));
  });

  it("lists sources with anchors, external links and dates", () => {
    expect(html).toContain('id="source-1"');
    expect(html).toContain('<a href="https://forum.example/p/1" target="_blank" rel="noopener noreferrer"');
    expect(html).toContain("forum.example · 2026-10-15");
    expect(html).toContain("Provided by you");
  });

  it("opens other links in a new tab and drops unsafe ones and raw HTML", () => {
    expect(html).toContain('<a href="https://docs.example/x" target="_blank" rel="noopener noreferrer">the docs</a>');
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("<script>");
  });

  it("shows the grounding note and no duplicate title or sources heading in the body", () => {
    expect(html).toContain("Grounding: 3 facts grounded");
    expect(html).not.toContain("Interview brief: Acme");
    expect(html.match(/>Sources</g)).toHaveLength(1);
  });
});
