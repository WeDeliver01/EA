# Terminal (Next.js)

The read-only portal terminal for DelicateTrader, built per
[`docs/specs/SPEC-09-frontend.md`](../docs/specs/SPEC-09-frontend.md)
against the read API in `backend/app/api/v1/`. See
[`docs/adr/0001-mvp-scope.md`](../docs/adr/0001-mvp-scope.md) (Phase 8)
for exactly what's built and what's deferred.

## Running it

From the repo root, `make up` builds and starts this alongside the
backend via `infra/docker-compose.yml` (see the root README). To run it
standalone against a backend already listening on `localhost:8000`:

```bash
npm install
npm run dev
```

`NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000/api/v1`) is
read at build time and baked into the client bundle - set it before
`npm run build`/`docker build`, not after.

## Layout

- `src/app/(dashboard)/` - the authenticated routes: `/` (dashboard),
  `/signals`, `/analysis/[id]`, `/positions`, `/trades`, `/system`.
- `src/app/login/` - the one unauthenticated route.
- `src/lib/api.ts` - the typed API client, token refresh, and every
  domain type the backend returns.
- `src/lib/auth.ts` - token storage (`localStorage`, not httpOnly
  cookies - a deliberate single-operator tradeoff, see the ADR).
- `src/components/` - shared UI: badges, panels, the status bar, nav.
