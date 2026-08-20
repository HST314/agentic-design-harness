import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: "http://127.0.0.1:18180",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run build && npm run preview",
    port: 18180,
    reuseExistingServer: !process.env.CI,
  },
});
