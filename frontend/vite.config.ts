import { defineConfig } from "vite";

export default defineConfig(() => {
  const backendUrl = process.env.HARNESS_BACKEND_URL ?? "http://127.0.0.1:18080";
  const securityHeaders = {
    "Content-Security-Policy": [
      "default-src 'self'",
      "base-uri 'self'",
      "object-src 'none'",
      "frame-ancestors 'none'",
      "frame-src http://127.0.0.1:* http://localhost:*",
      "img-src 'self' data: blob:",
      "connect-src 'self' ws://127.0.0.1:* ws://localhost:*",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "form-action 'self'",
    ].join("; "),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "X-Content-Type-Options": "nosniff",
  };
  const proxy = {
    "/api": backendUrl,
    "/healthz": backendUrl,
    "/readyz": backendUrl,
  };

  return {
    server: {
      strictPort: true,
      headers: securityHeaders,
      proxy,
    },
    preview: {
      strictPort: true,
      headers: securityHeaders,
      proxy,
    },
  };
});
