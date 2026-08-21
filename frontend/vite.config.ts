import { defineConfig } from "vite";

export default defineConfig(() => {
  const backendUrl = process.env.HARNESS_BACKEND_URL ?? "http://127.0.0.1:18080";
  const proxy = {
    "/api": backendUrl,
    "/healthz": backendUrl,
    "/readyz": backendUrl,
  };

  return {
    server: {
      strictPort: true,
      proxy,
    },
    preview: {
      strictPort: true,
      proxy,
    },
  };
});
