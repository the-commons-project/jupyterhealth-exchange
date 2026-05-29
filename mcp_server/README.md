# JHE MCP Server

The JHE MCP Server (`jhe_mcp`) exposes JupyterHealth Exchange (JHE) data to LLM clients via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). It acts as an **OAuth broker**: it presents an OAuth 2.0 Authorization Server interface to MCP clients while delegating the actual authentication to JHE (powered by django-oauth-toolkit + OIDC). The user logs in at the JHE instance; the MCP server forwards the resulting JHE-issued tokens and proxies data requests on the user's behalf. Because JHE enforces per-user RBAC, each user sees only the studies, patients, and observations they are authorized to access — the MCP server inherits those boundaries automatically.

The server is deployed at **`https://jhe-mcp.fly.dev`** (Fly app `jhe-mcp`).

---

## Architecture

```
LLM Client (e.g. Claude Desktop)
        │  MCP over HTTP/SSE
        ▼
┌─────────────────────────────────────┐
│         JHE MCP Server              │
│  OAuth Broker (Authorization façade)│
│  MCP Tools (studies, patients, obs) │
└────────────┬────────────────────────┘
             │  OAuth 2.0 + REST API calls
             ▼
┌─────────────────────────────────────┐
│       JupyterHealth Exchange        │
│  Authorization Server (django-oauth-│
│  toolkit + OIDC + PKCE)             │
│  RBAC enforced per user             │
│  Data: studies / patients / obs     │
└─────────────────────────────────────┘
```

**Flow:**
1. The LLM client connects to the MCP server's SSE endpoint and initiates OAuth.
2. The MCP server redirects the user to JHE's login page.
3. The user authenticates at JHE; JHE issues an authorization code.
4. The MCP server exchanges the code for JHE tokens and hands them back to the client. The server is **stateless** — it stores no tokens; the client holds and refreshes them.
5. Subsequent MCP tool calls carry the user's token, which the server forwards to JHE REST API requests — RBAC is enforced entirely by JHE.

---

## Registering the OAuth Client in JHE

The MCP server must be registered as an OAuth 2.0 confidential client in the JHE instance it will talk to. There are two ways to do this. In the URLs below, replace `<jhe-host>` with your JHE instance's host (for example, the host where JupyterHealth Exchange is deployed).

> **Important:** Do **not** use JHE's portal "Clients" page for this. That page creates a public client with a fixed `{SITE_URL}/auth/callback` redirect URI and cannot issue a `client_secret`. Use one of the two admin paths below instead.

### Option A — Self-service (any admin/staff user)

Navigate to:
```
https://<jhe-host>/o/applications/register/
```

Log in with a staff or superuser account, fill in the fields from the table below, and save.

### Option B — Django admin (superuser)

Navigate to:
```
https://<jhe-host>/admin/oauth2_provider/application/add/
```

Fill in the same fields from the table below. The admin form also exposes a **User** field — set it to the admin user creating the record, or leave it blank.

### Field Values

| Field | Value |
|---|---|
| Name | `JHE MCP Server` |
| Client type | `Confidential` |
| Authorization grant type | `Authorization code` |
| Redirect URIs | `https://jhe-mcp.fly.dev/oauth/callback` |
| Algorithm | `RSA with SHA-256 (RS256)` |
| Post logout redirect URIs | *(leave blank)* |
| Allowed origins | *(leave blank)* |
| User *(admin form only)* | the admin user creating it, or blank |

> **Copy the `client_secret` immediately after saving.** django-oauth-toolkit 3.x hashes the secret on save and never displays it again. If you lose it, you must regenerate a new one.

> **PKCE (S256)** is enforced globally via the `PKCE_REQUIRED` setting in JHE — it is not a per-application field and does not need to be configured here.

### Setting Fly Secrets

After registering, store the credentials as Fly secrets for the `jhe-mcp` app:

```bash
fly secrets set -a jhe-mcp \
  JHE_CLIENT_ID=<client_id> \
  JHE_CLIENT_SECRET=<client_secret> \
  MCP_BROKER_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))") \
  MCP_RESOURCE_URL=https://jhe-mcp.fly.dev
```

---

## Connecting an LLM Client

The server speaks the MCP **SSE transport** and uses OAuth with a **static, pre-registered `client_id`** (no Dynamic Client Registration). The universal way to connect a desktop client is the [`mcp-remote`](https://github.com/geelen/mcp-remote) stdio bridge: it runs the JHE OAuth login in the browser using the static client and forwards MCP over the remote SSE connection. Pass the JHE client with `--static-oauth-client-info` and force SSE with `--transport sse-only`:

```bash
npx -y mcp-remote https://jhe-mcp.fly.dev/sse \
  --transport sse-only \
  --static-oauth-client-info '{"client_id":"<client_id>","client_secret":"<client_secret>"}'
```

Our JHE client is **confidential**, so pass both `client_id` and `client_secret`. The first run opens a JHE login in the browser; `mcp-remote` caches the token under `~/.mcp-auth/` and refreshes it automatically. (Tip: instead of inlining the JSON you can use `--static-oauth-client-info @/absolute/path/to/client.json` to avoid shell-quoting.)

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "jhe": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://jhe-mcp.fly.dev/sse",
               "--transport", "sse-only",
               "--static-oauth-client-info", "{\"client_id\":\"<client_id>\",\"client_secret\":\"<client_secret>\"}"]
    }
  }
}
```

Restart Claude Desktop after editing.

### Claude Code

Claude Code's *native* remote-MCP OAuth (`claude mcp add --transport sse …`) currently requires Dynamic Client Registration, which JHE does not offer — so use the `mcp-remote` bridge:

```bash
claude mcp add jhe -- npx -y mcp-remote https://jhe-mcp.fly.dev/sse \
  --transport sse-only \
  --static-oauth-client-info '{"client_id":"<client_id>","client_secret":"<client_secret>"}'
```

Verify with `claude mcp list` and `claude mcp get jhe`.

### Google Gemini (Gemini CLI)

`~/.gemini/settings.json` — same `mcp-remote` bridge:

```json
{
  "mcpServers": {
    "jhe": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://jhe-mcp.fly.dev/sse",
               "--transport", "sse-only",
               "--static-oauth-client-info", "{\"client_id\":\"<client_id>\",\"client_secret\":\"<client_secret>\"}"]
    }
  }
}
```

(Gemini CLI can also reach the SSE endpoint natively via a `url` + `headers` entry if you already hold a JHE access token, but the bridge handles the browser login for you.)

### ChatGPT (OpenAI)

- **Responses / Agents API — supported.** Obtain a JHE access token yourself, then pass it on the `mcp` tool (the API does not manage OAuth for you):
  ```json
  {"type":"mcp","server_label":"jhe","server_url":"https://jhe-mcp.fly.dev/sse","authorization":"<JHE access token>","require_approval":"never"}
  ```
- **ChatGPT app "Connectors" (developer mode) — may work, unverified.** The server exposes the discovery metadata ChatGPT needs (`/.well-known/oauth-protected-resource` + `/.well-known/oauth-authorization-server`) and supports static clients, but ChatGPT connectors prefer CIMD/DCR; the static-client path here is not yet verified. The API path above is the reliable one.

> **Not supported:** clients that require Dynamic Client Registration with no static-client fallback — notably **Claude.ai web** connectors. Use a desktop client with `mcp-remote` instead.

---

## Configuration

The server is configured entirely via environment variables (or Fly secrets in production).

| Variable | Required | Purpose |
|---|---|---|
| `JHE_BASE_URL` | Yes | Base URL of the JHE instance (e.g. `https://jhe.fly.dev`) |
| `JHE_CLIENT_ID` | Yes | OAuth client ID issued by JHE when registering the application |
| `JHE_CLIENT_SECRET` | Yes | OAuth client secret issued by JHE (copy immediately — hashed on save) |
| `MCP_RESOURCE_URL` | Yes | Public URL of this MCP server (e.g. `https://jhe-mcp.fly.dev`) |
| `MCP_BROKER_KEY` | Yes | Random secret used to encrypt OAuth state and authorization codes; generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `MCP_ALLOWED_REDIRECTS` | No | Comma-separated list of non-loopback redirect URIs to allow (for additional MCP client types) |
| `MCP_HTTP_PORT` | No | Port the HTTP server listens on (default: `8401`) |

---

## Local Development

### Setup

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Running Tests

```bash
uv run pytest tests/unit
```

### Running the Server

```bash
jhe-mcp-http
```

Set the required environment variables (see [Configuration](#configuration)) before starting, or create a `.env` file and load it into your shell.

---

## Tools

The MCP server exposes the following tools to LLM clients:

- **`get_study_count`** — Returns the total number of studies the authenticated user can access.
- **`list_studies`** — Lists all studies visible to the authenticated user, with key metadata.
- **`get_study_metadata`** — Retrieves detailed metadata for a specific study by ID.
- **`get_patient_demographics`** — Returns demographic information for patients in a given study.
- **`get_patient_observations`** — Fetches health observations (e.g. vitals, device data) for a patient in a study.
