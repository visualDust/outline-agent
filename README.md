# Outline Agent

Webhook-driven Outline comment agent.

This service listens for Outline comment webhooks, gathers document and thread context, optionally uses local tools or document actions, and replies back into the same comment thread.

## Demo

- Placeholder: add an asciinema / screen-recording GIF here that shows mention -> tool call progress -> final reply.

## Quick Start

Choose one of these three installation methods:

1. **PyPI install** — quickest way to try the agent locally
2. **Docker Compose** — convenient for a more persistent containerized deployment
3. **Editable development install** — `pip install -e .[dev]`

Requirements:

- An Outline instance with API access and webhooks enabled

### 1. Install from PyPI

Once the package is published, install it with:

```bash
pip install outline-agent
```

Then start it:

```bash
outline-agent start
```

On first start, if `~/.outline-agent/config.yaml` does not exist yet, the CLI creates it and exits so you can edit it.

Main local config location:

- `~/.outline-agent/config.yaml`

Main local runtime data location:

- `~/.outline-agent/data/`

By default the service binds to:

- `127.0.0.1:8787`

You can change host/port in `~/.outline-agent/config.yaml` or via CLI flags.

Mermaid validation is optional in local non-Docker installs. If the Mermaid validator is not available locally, the agent logs a warning at startup and simply skips Mermaid preflight checks instead of failing requests.

If you installed from PyPI and want local Mermaid validation, install a Mermaid CLI backend separately:

```bash
npm install -g @mermaid-js/mermaid-cli
```

Then verify:

```bash
mmdc --version
```

If the binary is not on your normal shell `PATH`, you can point the agent at it explicitly:

```bash
export OUTLINE_AGENT_MERMAID_CLI_PATH=/path/to/mmdc
```

### 2. Via Docker Compose

This is a convenient deployment path when you want the agent running in a container with mounted config and runtime data.

```bash
git clone https://github.com/visualDust/outline-agent
cd outline-agent
cp docker-compose.example.yml docker-compose.yml
mkdir -p config data
cp docker/config.yaml.example config/config.yaml
```

Then edit:

- `config/config.yaml` — your main container config file
- especially:
  - `outline.api_base_url`
  - `outline.api_key`
  - `outline.webhook_signing_secret`
  - `model_profiles`

Start the service:

```bash
docker compose up -d
```

The Docker image includes Mermaid validation dependencies by default, so document writes can automatically preflight Mermaid blocks before posting to Outline.

Docker deployment layout:

- host config directory: `./config`
- host runtime data directory: `./data`
- container config path: `/config/config.yaml`
- container runtime data root: `/data`
- default port mapping: `8787:8787`

The example Compose file mounts:

- `./config:/config`
- `./data:/data`

and sets:

- `OUTLINE_AGENT_CONFIG_PATH=/config/config.yaml`

You can still override selected YAML config values through `environment:` in `docker-compose.yml`.

### Prompt overrides in Docker

The container uses the mounted config directory as its config root.

If you mount:

- `./config:/config`

and set:

- `OUTLINE_AGENT_CONFIG_PATH=/config/config.yaml`

then prompt overrides should live under:

- `./config/prompts/user/...`
- `./config/prompts/internal/...`

For example:

```text
config/
├── config.yaml
└── prompts/
    ├── user/
    │   ├── 00_system.md
    │   ├── reply_policy.md
    │   └── packs/
    │       └── outline_style.md
    └── internal/
        ├── action_router_policy.md
        ├── tool_planner_policy.md
        └── ...
```

You do not need to rebuild the image just to override prompts. Editing files under the mounted `config/prompts/` tree is enough.

### 3. Editable development install

Clone the repository first:

```bash
git clone https://github.com/visualDust/outline-agent
cd outline-agent
```

Install in editable mode:

```bash
pip install -e .[dev]
pre-commit install
```

Optional but recommended for local Mermaid validation:

```bash
npm ci
```

That installs the repo-pinned Mermaid CLI dependency into `node_modules/.bin/mmdc`, which the agent will discover automatically.

Then run:

```bash
outline-agent start --reload
```

or:

```bash
./scripts/run-dev.sh
```

The config and runtime data locations are the same as the normal local install:

- config: `~/.outline-agent/config.yaml`
- runtime data: `~/.outline-agent/data/`

### Configuration loading order

Configuration is loaded in this order:

1. CLI flags
2. Environment variables
3. User config YAML at `~/.outline-agent/config.yaml`
4. Built-in defaults

For normal local development, `~/.outline-agent/config.yaml` is the main configuration file.
Exported environment variables can override values from that YAML config.
The repository's `.env.example` is only a reference file; `.env` is **not** auto-loaded by the app.

### Maintenance diagnostics

The CLI includes a workspace drift diagnostic command:

```bash
outline-agent doctor workspace-sync
```

To quickly verify that your current Outline config is usable, run:

```bash
outline-agent auth info
```

Useful flags:

- `--depth deep`
- `--fix`
- `--config-path ...`
- `--workspace-root ...`

Behavior notes and repair semantics are documented here:

- [`docs/doctor-workspace-sync-behavior.md`](docs/doctor-workspace-sync-behavior.md)

Rather than duplicating a long config example here, use:

- [`docker/config.yaml.example`](docker/config.yaml.example) for a commented full example
- [`docs/config.md`](docs/config.md) for the field-by-field configuration reference

For local installs, `outline-agent start` will create `~/.outline-agent/config.yaml` on first run if it does not exist yet.

If you want to apply overrides from a local `.env` file manually, one common shell pattern is:

```bash
set -a
source .env
set +a
outline-agent start
```

## Configuration Notes

### Outline API key vs webhook configuration

- `outline.api_key` should be created from the Outline user account you want the agent to act as.
- That API key determines the agent's runtime identity in Outline, including which user appears as the author of agent-created comments or documents.
- The service resolves that Outline user automatically at startup via `auth.info`; there is no separate `agent_user_id` config anymore.

- `outline.webhook_signing_secret` is different. Webhooks are typically created from an Outline admin account. The webhook signing secret is only used to verify that incoming webhook requests are genuine.
- It is **not** bound to the API key and does **not** determine the agent's Outline identity.

In short:

- API key => **who the agent is**
- Webhook signing secret => **which webhook requests are trusted**

If the API key is invalid or expired, startup will fail clearly. If a long-running instance later hits an auth failure, the cached runtime identity is cleared and subsequent requests will fail clearly until you update the key and restart or reload the service.

### Important config areas

- `server`: bind host and port
- `outline`: Outline base URL, API key, webhook signing secret
- `trigger`: mention/all mode, aliases, collection filtering
- `model`: default runtime model ref and request timeout (`timeout_seconds`)
- `model_profiles`: provider credentials and allowed model names
- `prompts`: system prompt overrides and prompt packs
- `features`: enable or disable memory, document updates, tools, reactions, progress comments, and related docs
- `runtime`: dry-run mode, planning/execution limits, and Mermaid validation behavior
- `logging`: log level and file path

### Mermaid validation behavior

- Mermaid validation is automatically triggered before `create_document` and `apply_document_update` when the drafted text contains Mermaid code fences.
- This is a write-time guardrail; the planner does not need to call a separate Mermaid validation tool.
- In Docker, Mermaid validation dependencies are included by default.
- In local installs, Mermaid validation is optional. If unavailable and `runtime.mermaid_validation_mode=auto`, the agent logs that validation is unavailable and continues without blocking the request.
- If you want local Mermaid validation in a repo checkout, run `npm ci`.
- If you installed from PyPI and are not in a repo checkout, install Mermaid CLI separately with `npm install -g @mermaid-js/mermaid-cli`.
- Advanced local setups can also point the agent at a compatible Mermaid CLI binary with `OUTLINE_AGENT_MERMAID_CLI_PATH=/path/to/mmdc`.
- Mermaid retries are counted per document write attempt, not per Mermaid block.
- `runtime.mermaid_validation_exhausted_action=allow_write` means that after the retry budget is exhausted, later document write attempts can bypass Mermaid validation and publish the current draft anyway.
- `runtime.mermaid_validation_exhausted_action=block` keeps the stricter behavior and continues blocking writes after retry exhaustion.

### Environment variable overrides

- Local development should normally use `~/.outline-agent/config.yaml` as the primary config file.
- Exported environment variables override values from that YAML config.
- `.env.example` is provided as a reference for variable names and common values.
- `.env` files are not auto-loaded by the service; if you want to use one, source it in your shell before starting the app.

### Tool loop budget

- `tool_execution_max_steps` is the planner budget.
- `tool_execution_chunk_size` is the small batch actually executed before replanning.

This keeps the runtime closer to a weak planner plus iterative tool loop.

### Prompt overrides

Packaged prompts live under:

```text
src/outline_agent/assets/prompts/
  user/
    00_system.md
    reply_policy.md
    packs/
      outline_style.md
  internal/
    action_router_policy.md
    memory_action_policy.md
    tool_planner_policy.md
    document_update_policy.md
    document_creation_policy.md
    memory_update_policy.md
    document_memory_update_policy.md
```

User-visible and style overrides can live under:

```text
~/.outline-agent/
  prompts/
    user/
      00_system.md
      reply_policy.md
      packs/
        outline_style.md
```

Maintainer-level internal prompt overrides can live under:

```text
~/.outline-agent/
  prompts/
    internal/
      action_router_policy.md
      memory_action_policy.md
      tool_planner_policy.md
      document_update_policy.md
      document_creation_policy.md
      memory_update_policy.md
      document_memory_update_policy.md
```

## Deployment Note

If the service is deployed inside a LAN or on a machine that Outline cannot reach directly, you usually need a way to expose the webhook endpoint externally, for example:

- reverse proxy or port forwarding
- tunnel service
- cloudflared

For example, one working setup is to run the agent locally and expose it through `cloudflared`, so the Outline webhook can reach your local `POST /outline/webhook` endpoint from outside the LAN.

## Endpoints

- `GET /`
- `GET /healthz`
- `POST /outline/webhook`

## Thread / Session Model

- In Outline, the agent treats a comment thread as the session boundary.
- A user should start a new chat by creating a new top-level comment.
- If the user is following up on the same conversation, they should reply inside that same thread.
- Posting a brand-new top-level comment is treated as starting a new conversation, not continuing the old one.

## Runtime Data

By default, runtime data is written under `~/.outline-agent/`.

Typical contents include:

- `data/webhooks/events.jsonl`
- `data/webhooks/last_event.json`
- `data/processed_events.json`
- `data/agents/...` collection workspaces
- `data/agents/.../threads/...` per-thread state and work directories

Relative runtime paths are resolved from the active config directory.

For the current runtime data structure, see [`docs/data-layout.md`](docs/data-layout.md).

## Documentation

- Tool and capability reference: [`docs/tools.md`](docs/tools.md)
- Runtime data layout: [`docs/data-layout.md`](docs/data-layout.md)
- Configuration reference: [`docs/config.md`](docs/config.md)
- Example config: [`config.example.yaml`](config.example.yaml)
- Environment variable reference: [`.env.example`](.env.example)
- Container config example: [`docker/config.yaml.example`](docker/config.yaml.example)
- Docker Compose example: [`docker-compose.example.yml`](docker-compose.example.yml)

## Notes

- Comment replies are intentionally short by default.
- If a full answer would be long, the agent should prefer a short comment reply and then offer to write or expand into an Outline document.
- Outline comments support limited markdown compared with Outline documents.
- The current tool and capability surface is documented in [`docs/tools.md`](docs/tools.md).
