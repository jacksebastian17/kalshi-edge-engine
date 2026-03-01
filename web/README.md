# Kalshi Edge Dashboard

Frontend for the Kalshi Edge Engine.

## Development

```bash
cd web
npm install
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
