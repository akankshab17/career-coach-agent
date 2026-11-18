# Interview Brief web app (`web/`)

Next.js (App Router, TypeScript strict, Tailwind) in front of the FastAPI service in `../src/api.py`. The browser only
talks to this server; its route handlers (`app/api/*`) call FastAPI with a shared internal key (`lib/fastapi.ts`).

## Run locally (Node 24)
```
# terminal 1, repo root: the Python API
INTERNAL_API_KEY=<key> .venv/bin/uvicorn --app-dir src --factory api:serve --port 8000

# terminal 2, web/
cp .env.example .env.local   # then fill in the same INTERNAL_API_KEY
npm install
npm run dev                  # http://localhost:3000, health: /api/health
```

## Try it without spending credits
`scripts/fake_api.py` runs the real FastAPI app with a scripted pipeline (no Tavily/LLM calls). Use it instead of
uvicorn in terminal 1:
```
INTERNAL_API_KEY=<key> FAKE_STEP_SECONDS=2 .venv/bin/python scripts/fake_api.py   # from the repo root
```
Company "Fail Co" shows the failed state; restarting it "expires" earlier briefs (fresh database), like the real
server after it sleeps; stopping it shows "waking up".

## Checks
Stop `npm run dev` before `npm run build`: both write `.next/`, and a build under a running dev server left it
reloading pages in a loop (fix: stop it, `rm -rf .next`, start again).
```
npm test            # Vitest
npm run typecheck   # tsc --noEmit
npm run lint        # ESLint, no warnings allowed
npm run build
npm run test:e2e    # Playwright: builds into .next-e2e, starts the fake API (8100) and Next (3100) itself
```
The end-to-end test (`e2e/brief.spec.ts`) logs in, builds a brief against `scripts/fake_api.py`, follows the live
progress, checks the citation chips, sources and download, and finds the brief in the history. First run:
`npx playwright install chromium`. It uses its own ports and build folder, so `npm run dev` can stay running.

CI (`.github/workflows/ci.yml`) runs pytest, typecheck, lint, Vitest and Playwright on every push and pull request,
with no secrets.

## Environment variables
| Var | Purpose |
|---|---|
| `FASTAPI_URL` | Where FastAPI is (default `http://localhost:8000`) |
| `INTERNAL_API_KEY` | Sent as `X-Internal-Key`; must match the API's value |
| `APP_ACCESS_CODE` | The login code (required: login fails closed without it) |
| `SESSION_SECRET` | 32+ chars; signs the httpOnly session cookie (`ib_session`, 7 days) |

## Login
`/login` posts the code to `/api/login`, which sets a signed session cookie. `proxy.ts` (Next 16's name for
middleware) sends visitors without one to `/login` (pages) or answers 401 (`/api/*`); route handlers check again
with `requireSession` (`lib/http.ts`). Open without login: `/login`, `/api/login`, `/api/health`.

## API routes (this server)
| Route | What it does |
|---|---|
| `POST /api/login` `{code}` | Sets the session cookie. 10 attempts per IP per 15 min |
| `POST /api/logout` | Clears it |
| `GET /api/health` | `{web, api}`: is FastAPI reachable (open, no login) |
| `GET /api/briefs/:id` | One brief's state (404 = no longer stored) |
| `GET /api/briefs/:id/events` | Server-Sent Events: `job` (on change), `waking`, `expired`, `fatal`. Polls FastAPI every 2s; each stream ends after ~50s and EventSource reconnects |
| `POST /api/briefs` | `{company, role, level?, location?, jd?, rerun?}` → zod (`lib/schema.ts`) → rate limit → FastAPI `POST /briefs`. 202 with the job; 400 bad input; 429 too many (`Retry-After`); 502/503 FastAPI problems |

Rate limits (`lib/ratelimit.ts`): 3 briefs per session per 10 min and 10 per IP per hour, counted only for valid
requests. They live in each server instance's memory (best effort on Vercel); FastAPI's `MAX_BRIEFS_PER_DAY` is the
real cap.

## Pages
- `/` the brief form (same zod schema as the server) and **Recent briefs** from this browser's localStorage
  (`lib/history.ts`, key `ib_history_v1`, max 20, never the JD text). Statuses are refreshed from the server on each
  visit (503s retried ~30s for cold starts); briefs the server no longer has show as **Expired** with
  **Build again**, which opens the form prefilled from the query string.
- `/briefs/:id` live progress (`components/Progress.tsx`: connecting, waking up, waiting in line, steps, failed,
  expired), then the brief (`components/BriefView.tsx`): numbered citation chips jump to the Sources list,
  external links open in a new tab, raw HTML is never rendered, **Download .md** gives the original markdown.

## Deploy on Vercel (Hobby, free)
Import the GitHub repo from `main` with **Root Directory** `web` (framework Next.js, defaults for build/output) and
**Node.js Version** `24.x` (`engines` pins it too). Set these for Production and Preview, marked **Sensitive**:

| Variable | Value |
|---|---|
| `FASTAPI_URL` | The Azure Container App URL (`https://...azurecontainerapps.io`, no trailing slash) |
| `INTERNAL_API_KEY` | The same key as the Container App secret `internal-api-key` (see the repo README) |
| `APP_ACCESS_CODE` | A new random code, 16+ characters (`openssl rand -base64 18`) |
| `SESSION_SECRET` | `openssl rand -hex 32`; changing it logs everyone out |

On Vercel (`VERCEL=1`) the server refuses an access code shorter than 16 characters and a `FASTAPI_URL` that is not
`https://` (500, logged; `/api/health` shows `web: misconfigured`). Env changes apply only after a redeploy; change
`INTERNAL_API_KEY` on Azure and Vercel together.
