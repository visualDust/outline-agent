# Outline Agent

Webhook-driven Outline comment agent.

This service listens for Outline comment webhooks, gathers document/thread context, optionally uses local tools or document actions, and replies back into the same comment thread.

## Demo

- Placeholder: add an asciinema / screen-recording GIF here that shows mention -> tool call progress -> final reply.

## Documentation

- Tool and capability reference: [`docs/tools.md`](docs/tools.md)
- Example config: [`config.example.yaml`](config.example.yaml)

## Install

Requirements:

- Python 3.11+
- An Outline instance with API access and webhooks enabled

Install in editable mode:

```bash
pip install -e .[dev]
pre-commit install
```

## Configuration

Configuration is loaded in this order:

1. CLI flags
2. Environment variables
3. User config YAML at `~/.outline-agent/config.yaml`
4. Built-in defaults

On first start, if `~/.outline-agent/config.yaml` does not exist yet, the CLI creates it and exits so you can edit it.

### Minimal config

```yaml
server:
  host: 127.0.0.1
  port: 8787

outline:
  api_base_url: https://outline.example.com/api
  api_key: ol_api_0123456789abcdef0123456789abcdef
  webhook_signing_secret: ol_whs_0123456789abcdef0123456789abcdef

trigger:
  mode: mention
  mention_aliases:
    - "@agent"

model:
  ref: null
  timeout_seconds: 180

model_profiles:
  default: demo/gpt-4.1-mini
  profiles:
    demo:
      provider: openai-responses
      base_url: https://your-gateway.example.com/openai/v1
      api_key: ""
      models:
        - gpt-4.1-mini

prompts:
  system_prompt_packs:
    - outline_style

features:
  memory_actions: true
  memory_updates: true
  document_updates: true
  tool_use: true
  thread_sessions: true
  reactions: true
  related_documents: true
  progress_comments: true

runtime:
  dry_run: false
  tool_execution_max_rounds: 10
  tool_execution_max_steps: 6
  tool_execution_chunk_size: 2

logging:
  level: DEBUG
```

See [`config.example.yaml`](config.example.yaml) for a fuller example.

`tool_execution_max_steps` is the planner budget, while `tool_execution_chunk_size` is the small batch actually executed before replanning. This keeps the architecture closer to a weak planner + iterative tool loop.

### Outline API key vs webhook configuration

- `outline.api_key` should be created from the Outline user account you want the agent to act as.
- That API key determines the agent's runtime identity in Outline, including which user appears as the author of agent-created comments or documents.
- The service resolves that Outline user automatically at startup via `auth.info`; there is no separate `agent_user_id` config anymore.

- `outline.webhook_signing_secret` is different. Webhooks are typically created from an Outline admin account. The webhook signing secret is only used to verify that incoming webhook requests are genuine.
- It is **not** bound to the API key and does **not** determine the agent's Outline identity.

In short:

- API key => **who the agent is**
- Webhook signing secret => **which webhook requests are trusted**

If the API key is invalid or expired, startup will fail clearly. If a long-running instance later hits an auth failure (for example because the key expired after a month), the cached runtime identity is cleared and subsequent requests will fail clearly until you update the key and restart/reload the service.

### Important config areas

- `server`: bind host and port
- `outline`: Outline base URL, API key, webhook signing secret
- `trigger`: mention/all mode, aliases, collection filtering
- `model`: default runtime model ref and request timeout (`timeout_seconds`)
- `model_profiles`: provider credentials and allowed model names
- `prompts`: system prompt overrides and prompt packs
- `features`: enable/disable memory, document updates, tools, reactions, progress comments, related docs
- `runtime`: dry-run mode and planning/execution limits
- `logging`: log level and file path

### Prompt overrides

Packaged prompts live under `src/outline_agent/assets/prompts/`.

User overrides can live under:

```text
~/.outline-agent/
  prompts/
    00_system.md
    packs/
      outline_style.md
```

## Run

Start via the CLI:

```bash
outline-agent start
```

Common variants:

```bash
outline-agent start --reload
python -m outline_agent start
uvicorn outline_agent.app:app --host 127.0.0.1 --port 8787 --reload
```

If you use the helper script:

```bash
./scripts/run-dev.sh
```

## Deployment note

If the service is deployed inside a LAN or on a machine that Outline cannot reach directly, you usually need some way to expose the webhook endpoint externally, for example:

- reverse proxy / port forwarding
- tunnel service
- cloudflared

For example, one working setup is to run the agent locally and expose it through `cloudflared`, so the Outline webhook can reach your local `POST /outline/webhook` endpoint from outside the LAN.

## Endpoints

- `GET /`
- `GET /healthz`
- `POST /outline/webhook`

## Runtime data

By default, runtime data is written under `~/.outline-agent/`.

Typical contents:

- `data/webhooks/events.jsonl`
- `data/webhooks/last_event.json`
- `data/processed_events.json`
- `data/agents/...` collection workspaces
- `data/agents/.../threads/...` per-thread state and work dirs

Relative runtime paths are resolved from the active config directory.

## Thread / session model

- In Outline, the agent treats a comment thread as the session boundary.
- A user should start a new chat by creating a new top-level comment.
- If the user is following up on the same conversation, they should reply inside that same thread.
- Posting a brand-new top-level comment is treated as starting a new conversation, not continuing the old one.

## Notes

- Comment replies are intentionally short by default.
- If a full answer would be long, the agent should prefer a short comment reply and then offer to write or expand into an Outline document.
- Outline comments support limited markdown compared with Outline documents.
- The current tool/capability surface is documented in [`docs/tools.md`](docs/tools.md).