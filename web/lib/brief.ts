// Turns a finished brief's markdown (src/brief.py finalize_brief) into what the page shows: the body, the numbered
// sources, and the grounding note. Safe to import in the browser.
//
// The format: "# Interview brief: ..." title, sections, citations like [3](url) (several in a row for merged facts;
// a bare [3] when the source is the pasted job description), then "## Sources" with lines
// "3. [Title](url) (2026-10-15)" or "3. Job description provided by you", then an italic "_Grounding: ..._" note.

export type Source = { n: number; title: string; url: string | null; date: string | null };

export type ParsedBrief = { body: string; sources: Source[]; note: string | null };

const SOURCES_HEADING = /^##\s+(?:\d+\.\s*)?Sources\s*$/m;
// URLs may hold one level of balanced parentheses (Wikipedia's ".../Foo_(bar)").
const SOURCE_LINE = /^(\d+)\.\s+(?:\[((?:\\.|[^\]\\])+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)(?:\s+\(([^)]+)\))?|(.+))$/;
const NOTE = /^_(Grounding:.*)_\s*$/m;

export function parseBrief(markdown: string): ParsedBrief {
  let text = markdown.replace(/\r\n/g, "\n");
  text = text.replace(/^# .*\n+/, ""); // the page shows its own title

  let note: string | null = null;
  const noteMatch = text.match(NOTE);
  if (noteMatch) {
    note = noteMatch[1];
    text = text.replace(NOTE, "");
  }

  const sources: Source[] = [];
  const heading = text.match(SOURCES_HEADING);
  if (heading?.index !== undefined) {
    const after = text.slice(heading.index + heading[0].length);
    const nextSection = after.search(/^##\s/m);
    const block = nextSection === -1 ? after : after.slice(0, nextSection);
    for (const line of block.split("\n")) {
      const m = line.trim().match(SOURCE_LINE);
      if (!m) continue;
      if (m[2] !== undefined) {
        sources.push({ n: Number(m[1]), title: m[2].replace(/\\([[\]])/g, "$1"), url: safeUrl(m[3]), date: m[4] ?? null });
      } else {
        sources.push({ n: Number(m[1]), title: m[5].trim(), url: null, date: null });
      }
    }
    text = text.slice(0, heading.index) + (nextSection === -1 ? "" : after.slice(nextSection));
  }

  return { body: joinCitations(linkBareCitations(sentencesAsParagraphs(text.trim()))), sources, note };
}

/**
 * The writer puts each sentence on its own line, and markdown would join consecutive lines into one paragraph (a wall
 * of text). A blank line between two plain-text lines keeps one sentence per paragraph; lists, headings and other
 * block syntax are left alone.
 */
function sentencesAsParagraphs(body: string): string {
  const plain = (line: string) => line.trim() !== "" && !/^\s*([-*+>|#]|\d+[.)]\s|```)/.test(line);
  const lines = body.split("\n");
  return lines.map((line, i) => (i > 0 && plain(line) && plain(lines[i - 1]) ? `\n${line}` : line)).join("\n");
}

/** "[3]" not followed by "(" (a citation of the pasted JD) becomes a link to its source entry, like the others. */
function linkBareCitations(body: string): string {
  return body.replace(/\[(\d+)\](?![(\[:])/g, "[$1](#source-$1)");
}

/** A word joiner (U+2060) between adjacent citations, so "[3][4]" never wraps onto two lines. */
function joinCitations(body: string): string {
  return body.replace(/(\[\d+\]\((?:[^()\s]|\([^()\s]*\))+\))(?=\[\d+\]\()/g, "$1\u2060");
}

/** Only http(s) links are rendered as links (page text is third-party content). */
export function safeUrl(url: string | undefined | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}

/** A citation link's text is just its number. */
export function citationNumber(text: string): number | null {
  return /^\d+$/.test(text.trim()) ? Number(text.trim()) : null;
}

export function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Same name the Streamlit app used: company-role-level.md, lowercase, dashes. */
export function briefFileName(job: { company: string; role: string; level: string }): string {
  const slug = `${job.company}-${job.role}-${job.level}`.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return `${slug || "brief"}.md`;
}
