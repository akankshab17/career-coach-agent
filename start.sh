#!/usr/bin/env bash
# Starts the API on 127.0.0.1:8000 (not reachable from outside the container), waits until it answers,
# then starts Streamlit on 0.0.0.0:$PORT. If either process exits, the container exits, so the platform restarts it.
set -euo pipefail

PORT="${PORT:-7860}"                # Render sets PORT (render.yaml: 10000)
STARTUP_WAIT="${STARTUP_WAIT:-180}"  # seconds; Render's free instance has 0.1 CPU, so imports take a while

uvicorn --app-dir src --factory api:serve --host 127.0.0.1 --port 8000 &
api_pid=$!
trap 'kill "$api_pid" "${app_pid:-}" 2>/dev/null || true' TERM INT

api_up=0
for _ in $(seq 1 "$STARTUP_WAIT"); do
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "[start] the API exited during startup (see the error above; is APP_ACCESS_CODE set?)" >&2
    exit 1
  fi
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" 2>/dev/null; then
    api_up=1
    break
  fi
  sleep 1
done
if [ "$api_up" != 1 ]; then
  echo "[start] the API did not answer within ${STARTUP_WAIT}s" >&2
  kill "$api_pid" 2>/dev/null || true
  exit 1
fi

streamlit run app.py --server.port "$PORT" --server.address 0.0.0.0 --server.headless true \
  --browser.gatherUsageStats false &
app_pid=$!

wait -n "$api_pid" "$app_pid"
echo "[start] a process exited; stopping the container" >&2
kill "$api_pid" "$app_pid" 2>/dev/null || true
exit 1
