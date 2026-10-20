"""CLI entry point: python src/main.py --company Stripe --role "Backend Engineer" """
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

import llm
from brief import BriefResult, RunStats
from graph import run_pipeline
from location import DEFAULT_LOCATION
from search import LEVELS


def run(company: str, role: str, level: str, jd: str | None, use_cache: bool = True, stats: RunStats | None = None,
        location: str = DEFAULT_LOCATION) -> BriefResult:
    return run_pipeline(company, role, level, jd, use_cache=use_cache, stats=stats, location=location)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Career Coach Research Agent")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--level", choices=LEVELS, default="new-grad", help="Seniority to target (default: new-grad)")
    parser.add_argument("--location", default=DEFAULT_LOCATION,
                        help=f'Where the job should be; postings elsewhere are avoided (default: "{DEFAULT_LOCATION}")')
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
    stats = RunStats()
    debug_path = args.debug_file or (os.path.splitext(args.output)[0] + ".debug.md" if args.output else "last_run.debug.md")
    try:
        result = run(args.company, args.role, args.level, args.jd, use_cache=not args.no_cache, stats=stats, location=args.location)
    except llm.LLMError as exc:  # the run stopped early: keep what it logged, say why, and exit non-zero
        with open(debug_path, "w") as f:
            f.write(stats.debug_report() + f"\n# Run stopped\n\n{exc}\n")
        print(llm.summary_line(), file=sys.stderr)
        print(f"[debug] partial run report in {debug_path}", file=sys.stderr)
        sys.exit(f"\nRun stopped: {exc}")
    if args.output:
        with open(args.output, "w") as f:
            f.write(result.text)
        print(f"Wrote {args.output}")
    else:
        print(result.text)

    with open(debug_path, "w") as f:
        f.write(result.stats.debug_report(result.sources))
    print(f"[debug] details in {debug_path}", file=sys.stderr)
    print(llm.summary_line(), file=sys.stderr)


if __name__ == "__main__":
    main()
