import { defineConfig, devices } from "@playwright/test";

// One end-to-end test against a production build and the fake pipeline (../scripts/fake_api.py: the real FastAPI
// app with a scripted pipeline, no Tavily/LLM calls). Own ports and build folder, so it can run next to `npm run dev`.
// The secrets below are test-only values, not real keys.

const API_PORT = 8100;
const WEB_PORT = 3100;
const INTERNAL_API_KEY = "e2e-internal-key-not-a-secret-000000000000";
export const ACCESS_CODE = "e2e-access-code";
const PYTHON = process.env.PYTHON ?? "../.venv/bin/python"; // CI sets PYTHON=python

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `${PYTHON} ../scripts/fake_api.py`,
      url: `http://127.0.0.1:${API_PORT}/health`,
      env: { INTERNAL_API_KEY, FAKE_API_PORT: String(API_PORT), FAKE_STEP_SECONDS: "0.4" },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npx next build && npx next start --port ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}/login`,
      env: {
        NEXT_DIST_DIR: ".next-e2e",
        FASTAPI_URL: `http://127.0.0.1:${API_PORT}`,
        INTERNAL_API_KEY,
        APP_ACCESS_CODE: ACCESS_CODE,
        SESSION_SECRET: "e2e-session-secret-not-a-secret-0000000000",
      },
      reuseExistingServer: false,
      timeout: 240_000,
    },
  ],
});
