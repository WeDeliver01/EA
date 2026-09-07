# Nginx vhost config (not yet built)

Production reverse-proxy config (WebSocket upgrade, agent long-poll
timeouts, security headers, rate limiting) per
`docs/specs/SPEC-08-infrastructure.md` §3. There is nothing in `execution/`
or `workers/` running yet to put behind it - see `docs/adr/0001-mvp-scope.md`.
The dev stack (`infra/docker-compose.yml`) exposes the API directly on
127.0.0.1:8000 instead.
