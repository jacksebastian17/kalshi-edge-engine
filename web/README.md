# Kalshi Edge Dashboard

Frontend for the Kalshi Edge Engine. During an isolated experiment, point the dashboard at the single backend started by the overnight launcher. The paper diagnostics API exposes receipt references, and the offline verifier resolves them after backup. [Research procedure](../docs/phase-b3b1.md)

## Development

```bash
cd web
npm ci
npm run dev
```

The dashboard will be available at `http://localhost:5173`.

## Production Build

```bash
npm run build
```

## Environment

Configure the API base URL via environment variables:

```env
VITE_API_BASE_URL=http://localhost:8000
```

Defaults to `http://localhost:8000` if not set.
