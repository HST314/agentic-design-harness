import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "real-stack.spec.ts",
  use: {
    baseURL: "http://127.0.0.1:18180",
    trace: "retain-on-failure",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : undefined,
  },
  webServer: {
    command: "npm run build && npm run preview:local",
    port: 18180,
    reuseExistingServer: !process.env.CI,
  },
});
