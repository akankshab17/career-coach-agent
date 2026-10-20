"""CLI entry point: python src/main.py --company Stripe --role "Backend Engineer" """
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

import llm
from brief import BriefResult
from graph import run_pipeline
from search import LEVELS


def run(company: str, role: str, level: str, jd: str | None, use_cache: bool = True) -> BriefResult:
    return run_pipeline(company, role, level, jd, use_cache=use_cache)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Career Coach Research Agent")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--level", choices=LEVELS, default="new-grad", help="Seniority to target (default: new-grad)")
    parser.add_argument("--jd", help="Job description text (optional)")
    parser.add_argument("-o", "--output", help="Write the brief to this file instead of stdout")
    parser.add_argument(
        "--debug-file",
        help="Where to write the run report: step counts, rejected facts, dropped sentences "
             "(default: <output>.debug.md, or last_run.debug.md)",
    )
    parser.add_argument("--no-cache", action="store_true", help="Re-run fact extraction instead of reading .cache/extract")
    args = parser.parse_args(argv)

    load_dotenv()
    llm.reset_usage()
    try:
        result = run(args.company, args.role, args.level, args.jd, use_cache=not args.no_cache)
    except llm.LLMError as exc:
        sys.exit(f"Error: {exc}")
    if args.output:
        with open(args.output, "w") as f:
            f.write(result.text)
        print(f"Wrote {args.output}")
    else:
        print(result.text)

    debug_path = args.debug_file or (os.path.splitext(args.output)[0] + ".debug.md" if args.output else "last_run.debug.md")
    with open(debug_path, "w") as f:
        f.write(result.stats.debug_report(result.sources))
    print(f"[debug] details in {debug_path}", file=sys.stderr)
    print(llm.summary_line(), file=sys.stderr)


if __name__ == "__main__":
    main()
