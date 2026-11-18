import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { citationNumber, hostname, parseBrief, type Source } from "@/lib/brief";

// A finished brief: the markdown body with numbered citation chips, then the sources they jump to. Raw HTML in the
// markdown is not rendered (react-markdown's default), and links are limited to http(s) and in-page anchors.

export function BriefView({ markdown }: { markdown: string }) {
  const { body, sources, note } = parseBrief(markdown);
  const byNumber = new Map(sources.map((s) => [s.n, s]));

  const components: Components = {
    a({ href, children }) {
      const n = citationNumber(textOf(children));
      if (n !== null && byNumber.has(n)) return <CitationChip n={n} source={byNumber.get(n)!} />;
      if (href?.startsWith("#")) return <a href={href}>{children}</a>;
      if (!href || !/^https?:\/\//.test(href)) return <>{children}</>;
      return (
        <a href={href} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      );
    },
  };

  return (
    <div className="space-y-6">
      <article className="card prose prose-zinc max-w-none p-5 sm:p-8 dark:prose-invert prose-headings:scroll-mt-20 prose-headings:tracking-tight prose-h2:mt-8 prose-h2:mb-3 prose-h2:text-lg prose-h2:first:mt-0 prose-li:my-1 prose-p:my-2">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {body}
        </ReactMarkdown>
      </article>
      {sources.length > 0 && <SourceList sources={sources} />}
      {note && <p className="px-1 text-xs text-zinc-500">{note}</p>}
    </div>
  );
}

function CitationChip({ n, source }: { n: number; source: Source }) {
  return (
    <a
      href={`#source-${n}`}
      title={source.title}
      aria-label={`Source ${n}: ${source.title}`}
      className="citation mx-px inline-flex h-[1.15rem] min-w-[1.15rem] -translate-y-px items-center justify-center rounded bg-zinc-100 px-1 align-middle text-[0.7rem] font-semibold leading-none text-zinc-700 no-underline ring-1 ring-zinc-200 transition hover:bg-zinc-900 hover:text-white hover:ring-zinc-900 dark:bg-zinc-800 dark:text-zinc-200 dark:ring-zinc-700 dark:hover:bg-zinc-100 dark:hover:text-zinc-900"
    >
      {n}
    </a>
  );
}

function SourceList({ sources }: { sources: Source[] }) {
  return (
    <section aria-labelledby="sources-heading" className="card p-5 sm:p-8">
      <h2 id="sources-heading" className="text-lg font-semibold tracking-tight">
        Sources
      </h2>
      <ol className="mt-4 space-y-1">
        {sources.map((s) => (
          <li key={s.n} id={`source-${s.n}`} className="source-item -mx-2 flex scroll-mt-20 gap-3 rounded-lg px-2 py-2">
            <span className="mt-0.5 flex h-6 min-w-6 shrink-0 items-center justify-center rounded-md bg-zinc-100 px-1 text-xs font-semibold text-zinc-700 dark:bg-zinc-800 dark:text-zinc-200">
              {s.n}
            </span>
            <div className="min-w-0">
              {s.url ? (
                <a href={s.url} target="_blank" rel="noopener noreferrer" className="break-words font-medium hover:underline">
                  {s.title}
                </a>
              ) : (
                <span className="font-medium">{s.title}</span>
              )}
              <p className="mt-0.5 truncate text-sm text-zinc-500">
                {s.url ? hostname(s.url) : "Provided by you"}
                {s.date && ` · ${s.date}`}
              </p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function textOf(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textOf).join("");
  return "";
}
