# Outline Agent

Webhook-driven Outline comment agent.

This service listens for Outline comment webhooks, gathers document and thread context, optionally uses local tools or document actions, and replies back into the same comment thread.

## Demo

- Placeholder: add an asciinema / screen-recording GIF here that shows mention -> tool call progress -> final reply.

## Quick Start

Choose one of these three installation methods:

1. **Docker Compose** — recommended for the easiest deployment
2. **PyPI install** — `pip install outline-agent`
3. **Editable development install** — `pip install -e .[dev]`

Requirements:

- An Outline instance with API access and webhooks enabled

### 1. Via Docker Compose

This is the easiest deployment path.

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

### 2. Install from PyPI

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

Minimal config:

```yaml
server:
  # HTTP bind host for the local webhook service.
  host: 127.0.0.1
  # HTTP bind port for the local webhook service.
  port: 8787

outline:
  # Your Outline API base URL. `/api` is added automatically if omitted.
  api_base_url: https://outline.example.com/api
  # API key created from the Outline user account that should act as the agent.
  api_key: ol_api_0123456789abcdef0123456789abcdef
  # Webhook signing secret created in Outline admin webhook settings.
  webhook_signing_secret: ol_whs_0123456789abcdef0123456789abcdef
  # Optional: HTTP timeout for Outline API requests, in seconds.
  # timeout_seconds: 30

trigger:
  # `mention` = only respond when mentioned or replied to; `all` = respond to all comments.
  mode: mention
  # Mention aliases that can trigger the agent in comment text.
  mention_aliases:
    - "@agent"
  # Optional: plain-text fallback mention detection when structured mentions are missing.
  # mention_alias_fallback_enabled: false
  # Optional: replies to the agent can trigger without a fresh mention.
  # on_reply_to_agent: true

model:
  # Main model ref. `null` means “use model_profiles.default”.
  ref: null
  # Timeout for model API requests, in seconds.
  timeout_seconds: 180

model_profiles:
  # Default `alias/model-name` ref used when model.ref is null.
  default: demo/gpt-4.1-mini
  profiles:
    demo:
      # Provider adapter name used by the runtime.
      provider: openai-responses
      # Base URL for your model gateway / API service.
      base_url: https://your-gateway.example.com/openai/v1
      # API key for the model provider or gateway above.
      api_key: ""
      # Allowed model names under this alias. The first one is the default.
      models:
        - gpt-4.1-mini

prompts:
  # Built-in or custom prompt packs appended to the base system prompt.
  system_prompt_packs:
    - outline_style

features:
  # Enable collection memory action planning.
  memory_actions: true
  # Enable writing back durable collection memory.
  memory_updates: true
  # Enable Outline document updates.
  document_updates: true
  # Enable local/file/shell tool use.
  tool_use: true
  # Enable writing back document-local memory.
  document_memory: true
  # Enable reaction emoji updates during processing.
  reactions: true
  # Enable related-document retrieval within the same collection.
  related_documents: true
  # Enable progress comments such as “Working on it...”.
  progress_comments: true

runtime:
  # If true, simulate side effects without actually mutating Outline state.
  dry_run: false
  # Max number of plan/replan rounds for one request.
  tool_execution_max_rounds: 10
  # Max number of steps the planner may propose in one round.
  tool_execution_max_steps: 6
  # Number of steps actually executed before replanning.
  tool_execution_chunk_size: 2

logging:
  # Application log level.
  level: DEBUG
```

See [`config.example.yaml`](config.example.yaml) for a fuller example, and [`docs/config.md`](docs/config.md) for a field-by-field reference.

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
- `runtime`: dry-run mode and planning/execution limits
- `logging`: log level and file path

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
