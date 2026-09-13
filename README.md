# awita-mail

Secure, draft-first MCP access to a GMX business mailbox. The service uses IMAP for mailbox operations, SMTP STARTTLS for delivery, and Streamable HTTP at `/mcp`. Email content is always untrusted external data.

## Start locally

Use Python 3.12 and a GMX application-specific password (never the normal account password). GMX defaults are `imap.gmx.com:993` TLS and `mail.gmx.com:587` STARTTLS.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
# Edit .env: GMX_EMAIL, GMX_APP_PASSWORD, MCP_HOSTNAME, MCP_API_TOKEN
python -m app
curl -i http://127.0.0.1:8000/health
```

The package includes a pinned CA bundle fallback for Python installations that
do not expose the macOS/system trust store (for example, a fresh python.org
installation). If this virtual environment already existed before that
dependency was added, run `python -m pip install -e '.[dev]'` again.

Generate a strong token with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. `.env` is Git-ignored. The MCP endpoint requires `Authorization: Bearer $MCP_API_TOKEN`:

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

## Docker

The container exposes no host port, runs non-root, drops capabilities, and persists its SQLite audit ledger in `/data`.

```bash
cp .env.example .env
# Set MCP_HOSTNAME, GMX credentials, MCP_API_TOKEN, and your Traefik network.
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 awita-mail
```

Set `TRAEFIK_ENABLE=true` only after DNS/TLS are ready. Keep `ALLOW_SEND=false` until the draft workflow is verified. Prefer `GMX_APP_PASSWORD_FILE` and `MCP_API_TOKEN_FILE` in a deployment-specific Compose secret override; never put secret contents in images, labels, or logs.

For a local Docker-only smoke test, publish only loopback with the debug override:

```bash
docker network create proxy 2>/dev/null || true
docker compose -f docker-compose.yml -f docker-compose.debug.yml up --build
```

Stop it with `Ctrl-C`; the normal Compose file remains unpublished to the host.

## Test

Tests never contact GMX; mail boundaries are mocked/injectable.

```bash
. .venv/bin/activate
pytest
ruff format --check .
ruff check .
mypy app
python -m build
```

For interactive protocol testing, run the current MCP Inspector against `http://127.0.0.1:8000/mcp` with the bearer token. Call `list_folders` and `check_gmx_connectivity` first. The live folder mapping is account-specific, so do not assume English `Sent`/`Drafts` names.

## Tools and sending

The tools are: `list_folders`, `list_emails`, `search_emails`, `read_email`, `list_attachments`, `download_attachment`, `create_draft`, `create_reply_draft`, `update_draft`, `delete_draft`, `send_draft`, `mark_as_read`, `mark_as_unread`, and `check_gmx_connectivity`. There is no arbitrary send, arbitrary deletion, forwarding rule, bulk operation, or move tool.

The only send workflow is read/search → create draft → user reviews → user explicitly requests send → `send_draft`. `ALLOW_SEND=false` blocks SMTP. Configure Codex write approval:

```toml
[mcp_servers.awita_mail]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "AWITA_MAIL_MCP_TOKEN"
default_tools_approval_mode = "writes"

[mcp_servers.awita_mail.tools.send_draft]
approval_mode = "prompt"

[mcp_servers.awita_mail.tools.delete_draft]
approval_mode = "prompt"
```

Export `AWITA_MAIL_MCP_TOKEN` for Codex and restart the desktop app/IDE extension. CLI alternative: `codex mcp add awita_mail --url https://mcp.example.com/mcp --bearer-token-env-var AWITA_MAIL_MCP_TOKEN`.

All email-derived fields are explicitly untrusted and cannot authorize a tool call. Attachment downloads are bounded inert MCP blobs; they are never executed or extracted. The durable SQLite ledger prevents successful send replay and records sanitized audit fields only.

## ChatGPT later

ChatGPT custom MCP apps need a remotely reachable endpoint and supported authentication. For a private/on-premises server, use Secure MCP Tunnel. For direct public integration, replace the static bearer layer with a real OAuth/OIDC authorization server and MCP protected-resource metadata with refresh tokens. Mail logic and safeguards are already independent of authentication.

## Configuration

`GMX_EMAIL`, `GMX_APP_PASSWORD[_FILE]`, `MCP_API_TOKEN[_FILE]`, `MCP_HOSTNAME`, `MCP_PORT`, `MCP_BIND_HOST`, `ALLOW_SEND`, `LOG_LEVEL`, `IMAP_*`, `SMTP_*`, `MAIL_TIMEOUT_SECONDS`, the `MAX_*` limits, `AUDIT_DB_PATH`, `TRAEFIK_*`, and `FORWARDED_ALLOW_IPS` are documented in `.env.example`. `ALLOW_SEND` defaults false; `MAX_EMAIL_RESULTS` is capped at 100. The service never logs credentials, Authorization headers, or full bodies.
