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

## Checks
```
npm test            # Vitest
npm run typecheck   # tsc --noEmit
npm run lint        # ESLint, no warnings allowed
npm run build
```

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
| `POST /api/briefs` | `{company, role, level?, location?, jd?, rerun?}` → zod (`lib/schema.ts`) → rate limit → FastAPI `POST /briefs`. 202 with the job; 400 bad input; 429 too many (`Retry-After`); 502/503 FastAPI problems |

Rate limits (`lib/ratelimit.ts`): 3 briefs per session per 10 min and 10 per IP per hour, counted only for valid
requests. They live in each server instance's memory (best effort on Vercel); FastAPI's `MAX_BRIEFS_PER_DAY` is the
real cap.
