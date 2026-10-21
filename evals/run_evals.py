"""Entry point: python evals/run_evals.py [--record | --from-facts] [--cases a,b] [--no-cache] [--no-fail] [--set-baseline] [--repeat N]

See evals/harness.py for what each mode does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
