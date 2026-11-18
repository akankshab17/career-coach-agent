"""Fill deploy/azure/app.template.yaml with secrets and settings, for `az containerapp create --yaml`.

    python deploy/azure/render_app.py --image ghcr.io/... --environment-id /subscriptions/... --out <private path>

Reads the API keys and model settings from .env (never printed). INTERNAL_API_KEY is generated fresh unless it is
already set in the environment (to keep the current one on a re-deploy). The output file is created readable by its
owner only; delete it right after use. Prints only which settings were included, never a value.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).with_name("app.template.yaml")
SECRETS = ("TAVILY_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY")
SETTINGS = ("LLM_PROVIDER", "LLM_MODEL", "EXTRACT_MODEL", "WRITE_MODEL", "FALLBACK_PROVIDER", "FALLBACK_EXTRACT_MODEL",
            "FALLBACK_WRITE_MODEL", "OPENAI_BUDGET_USD", "EXTRACT_REASONING_EFFORT", "WRITE_REASONING_EFFORT")


def render(template: str, values: dict[str, str], settings: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        # JSON string escaping is valid inside a YAML double-quoted scalar
        out = out.replace(f'"{{{{{key}}}}}"', json.dumps(value))
    env_lines = "".join(f"          - name: {k}\n            value: {json.dumps(v)}\n" for k, v in settings.items())
    out = out.replace("          # {{SETTINGS}}  (non-secret model settings copied from .env)\n", env_lines)
    if re.search(r'"\{\{[A-Z_]+\}\}"|# \{\{SETTINGS\}\}', out):
        raise ValueError("unfilled placeholder in the template")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args()

    env = {k: v for k, v in dotenv_values(args.env_file).items() if v}
    missing = [k for k in SECRETS if k not in env]
    if missing:
        print(f"missing in {args.env_file}: {', '.join(missing)}", file=sys.stderr)
        return 1
    values = {k: env[k] for k in SECRETS}
    values["INTERNAL_API_KEY"] = os.getenv("INTERNAL_API_KEY") or secrets.token_hex(32)
    values["IMAGE"] = args.image
    values["ENVIRONMENT_ID"] = args.environment_id
    settings = {k: env[k] for k in SETTINGS if k in env}

    text = render(TEMPLATE.read_text(), values, settings)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    print(f"wrote {args.out} (mode 600): secrets {', '.join(sorted(k for k in values if k.endswith('_KEY')))}; "
          f"settings {', '.join(settings) or 'none'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
