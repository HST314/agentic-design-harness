# Harness web shell

TypeScript/Vite control-plane shell for task navigation, FIFO approval inbox,
approval routing, verified resources, Token/cost/retry-budget visibility and
redacted configuration management. Professional Agent workflow pages remain
adapter-provided deep links.

```bash
npm ci
npm run check
npm run build
```

Browser tests live in `e2e/` and run against the Vite preview server with
`npm run test:e2e` after the Playwright Chromium runtime is installed. A CI or
sandbox may set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to a compatible executable.
