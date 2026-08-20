import { defineConfig } from "vite";

export default defineConfig({
  server: {
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:18080",
      "/healthz": "http://127.0.0.1:18080",
      "/readyz": "http://127.0.0.1:18080",
    },
  },
});
