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
