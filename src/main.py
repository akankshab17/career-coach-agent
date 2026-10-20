"""CLI entry point: python src/main.py --company Stripe --role "Backend Engineer" """
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

import llm
from brief import BriefResult, generate_brief
from search import LEVELS, gather_research


def run(company: str, role: str, level: str, jd: str | None) -> BriefResult:
    research = gather_research(company, role, level, jd)
    return generate_brief(company, role, level, jd, research)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Career Coach Research Agent")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--level", choices=LEVELS, default="new-grad", help="Seniority to target (default: new-grad)")
    parser.add_argument("--jd", help="Job description text (optional)")
    parser.add_argument("-o", "--output", help="Write the brief to this file instead of stdout")
    args = parser.parse_args(argv)

    load_dotenv()
    llm.reset_usage()
    try:
        result = run(args.company, args.role, args.level, args.jd)
    except llm.LLMError as exc:
        sys.exit(f"Error: {exc}")
    if args.output:
        with open(args.output, "w") as f:
            f.write(result.text)
        print(f"Wrote {args.output}")
    else:
        print(result.text)
    print(result.stats.log_line(), file=sys.stderr)
    print(llm.summary_line(), file=sys.stderr)


if __name__ == "__main__":
    main()
