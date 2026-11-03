import { defineConfig } from "@playwright/test";
import path from "node:path";

const repoRoot = path.resolve(process.cwd(), "..");

export default defineConfig({
  testDir: "./e2e",
  use: { baseURL: "http://127.0.0.1:5173", browserName: "chromium" },
  webServer: [
    {
      command: "uv run --project . python tests/fixtures/phase_two_web_server.py",
      cwd: repoRoot,
      url: "http://127.0.0.1:8000/api/projects?offset=0&limit=1",
      env: {
        UV_CACHE_DIR: path.join(repoRoot, ".uv-cache"),
        PYTHONPATH: repoRoot,
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "npm run dev",
      cwd: process.cwd(),
      port: 5173,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
