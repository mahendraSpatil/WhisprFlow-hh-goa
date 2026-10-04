# CodeLoop frontend

React + Vite + TypeScript UI for CodeLoop: start a run, watch the nine agents
progress over the run's WebSocket, and explore the data-flow graph.

```
npm install
npm run dev        # http://localhost:5173, proxies /api to the backend on :8000
npm test           # reducer tests (vitest)
npm run build      # typecheck + production build
```

Start the backend first (`cd ../backend && .venv/Scripts/python -m uvicorn app.main:app`).
Point the dev proxy elsewhere with `CODELOOP_BACKEND=http://host:port npm run dev`, or
skip the proxy and call a backend directly with `VITE_API_BASE`.

`?run=<id>` in the URL reattaches to a run after a reload and replays its events.

Layout: `src/api` (backend types and client), `src/state` (run reducer and the
WebSocket hook), `src/components` (top bar, stage tracker, graph canvas, log panel).
