# Configuration Reference

This document describes the **recommended public configuration surface** for Outline Agent.

It is intended to complement:

- [`config.example.yaml`](../config.example.yaml) for a commented example
- [`README.md`](../README.md) for quick-start setup

The fields listed here are the ones most users are expected to configure directly. The runtime also contains additional low-level tuning options, but those are intentionally omitted from the primary reference unless they are commonly useful.

---

## Configuration loading order

Configuration is loaded in this order:

1. CLI flags
2. Environment variables
3. User YAML config at `~/.outline-agent/config.yaml`
4. Built-in defaults

If the config file does not exist yet, `outline-agent start` creates a starter config and exits so you can edit it.

---

## Minimal example

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
  document_memory: true
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

---

## Section-by-section reference

## `server`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `server.host` | No | `127.0.0.1` | HTTP bind host for the webhook service. | Change this if you want to bind on a non-loopback interface. |
| `server.port` | No | `8787` | HTTP bind port for the webhook service. | Must match the port exposed by your proxy/tunnel setup. |

## `outline`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `outline.api_base_url` | Yes | `""` | Base URL of your Outline API. | If you omit `/api`, the runtime adds it automatically. |
| `outline.api_key` | Yes | `""` | API key for the Outline user that should act as the agent. | This determines the agent's runtime identity in Outline. |
| `outline.webhook_signing_secret` | Strongly recommended | `""` | Secret used to verify incoming webhook signatures. | Usually created from an Outline admin-managed webhook. Not tied to the API key identity. |
| `outline.timeout_seconds` | No | `30` | HTTP timeout for Outline API requests. | Increase this if your Outline instance is slow or remote. |

### Outline identity model

- `outline.api_key` determines **who the agent is** in Outline.
- `outline.webhook_signing_secret` determines **which webhook requests are trusted**.
- There is no separate `agent_user_id` config anymore. The service resolves the current Outline user automatically via `auth.info` at startup.

---

## `trigger`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `trigger.mode` | No | `mention` | Controls whether the agent responds only when triggered or to every comment. | Allowed values: `mention`, `all`. |
| `trigger.mention_aliases` | No | `[@agent]` | Mention strings that can trigger the agent. | Used in mention mode. |
| `trigger.mention_alias_fallback_enabled` | No | `false` | Enables plain-text alias matching when structured mentions are unavailable. | Useful for imperfect webhook payloads or copied text. |
| `trigger.on_reply_to_agent` | No | `true` | Allows replies to the agent's own thread comments to trigger processing without a fresh mention. | Important for follow-up turns in the same thread. |
| `trigger.collection_allowlist` | No | `[]` | Restricts the agent to specific collection IDs. | Empty means all collections are allowed. |

---

## `model`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `model.ref` | No | `null` | Main runtime model reference. | `null` means “use `model_profiles.default`”. |
| `model.timeout_seconds` | No | `180` | Timeout for model API requests. | Increase this for longer tasks such as PDF analysis or multi-step tool use. |
| `model.action_router_ref` | No | `null` | Optional model override for action routing. | Falls back to `model.ref`. |
| `model.memory_ref` | No | `null` | Optional model override for memory update work. | Falls back to `model.ref`. |
| `model.document_update_ref` | No | `null` | Optional model override for document update drafting. | Falls back to `memory_ref`, then `model.ref`. |
| `model.tool_ref` | No | `null` | Optional model override for tool planning. | Falls back to `memory_ref`, then `model.ref`. |
| `model.document_memory_ref` | No | `null` | Optional model override for document memory updates. | Falls back to `memory_ref`, then `model.ref`. |
| `model.max_output_tokens` | No | `800` | Maximum model output length for one request. | Useful if your provider enforces explicit token caps. |

### Model ref format

Model refs use this format:

```text
<profile-alias>/<model-name>
```

Examples:

- `demo/gpt-4.1-mini`
- `prod/o4-mini`

If you omit `model.ref`, the runtime uses `model_profiles.default`.

---

## `model_profiles`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `model_profiles.default` | Yes | none | Default model ref used when `model.ref` is null. | Must point to a configured profile alias and allowed model. |
| `model_profiles.profiles.<alias>.provider` | Yes | none | Provider adapter name. | Example: `openai-responses`. |
| `model_profiles.profiles.<alias>.base_url` | Yes | none | Base URL for your model gateway or provider endpoint. | Trailing slash is normalized. |
| `model_profiles.profiles.<alias>.api_key` | Yes | none | API key used for that model provider. | Must be non-empty for resolution to succeed. |
| `model_profiles.profiles.<alias>.models` | Yes | none | Allowed model names under that alias. | The first model in the list is treated as the default. |

---

## `prompts`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `prompts.system_prompt_packs` | No | `[outline_style]` | Prompt packs appended to the base user system prompt. | Good place for style and product-specific behavior tweaks. |
| `prompts.system_prompt_path` | No | auto-resolved | Override for the main user system prompt file. | Relative paths are resolved from the active config root. |
| `prompts.prompt_pack_dir` | No | auto-resolved | Override for user prompt pack lookup. | Relative paths are resolved from the active config root. |
| `prompts.internal_prompt_dir` | No | auto-resolved | Override for internal maintainer prompt files. | Relative paths are resolved from the active config root. |

Packaged prompts live under `src/outline_agent/assets/prompts/`, while user overrides typically live under `~/.outline-agent/prompts/`.

---

## `features`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `features.memory_actions` | No | `true` | Enables collection-level memory action planning. | Controls whether the agent may decide to add/update/remove collection memory. |
| `features.memory_updates` | No | `true` | Enables durable collection memory write-back after replies. | Turn this off if you want read-only collection memory. |
| `features.document_updates` | No | `true` | Enables drafting and applying Outline document updates. | Required for “write this into the document” workflows. |
| `features.tool_use` | No | `true` | Enables local tools such as file, shell, and attachment workflows. | Required for PDF export, attachment processing, local file edits, etc. |
| `features.document_memory` | No | `true` | Enables document-local memory updates. | Helps the runtime preserve document-scoped context over time. |
| `features.reactions` | No | `true` | Enables processing/done reaction emoji updates. | Purely UX-facing. |
| `features.related_documents` | No | `true` | Enables retrieval of related documents from the same collection. | Used as extra context for drafting and planning. |
| `features.progress_comments` | No | `true` | Enables progress comments during longer-running tasks. | Useful for tool loops and multi-step tasks. |
| `features.same_document_comments` | No | `true` | Enables lookup of other comment threads in the same document when needed. | Useful for cross-thread context recovery and handoffs. |

---

## `runtime`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `runtime.dry_run` | No | `false` | Simulates side effects without mutating Outline state. | Useful for testing plans and prompts safely. |
| `runtime.tool_execution_max_rounds` | No | `10` | Maximum number of planning/replanning rounds for one request. | Increase this for more complex tool-driven tasks. |
| `runtime.tool_execution_max_steps` | No | `6` | Maximum number of steps the planner may propose in one round. | This is the planner budget, not the total global task budget. |
| `runtime.tool_execution_chunk_size` | No | `2` | Number of planned steps actually executed before replanning. | Smaller chunks improve adaptability after partial failures. |
| `runtime.workspace_root` | No | `data/agents` (relative) | Root directory for collection workspaces. | Relative paths are resolved from the active config root. |
| `runtime.webhook_log_dir` | No | `data/webhooks` (relative) | Directory for raw webhook event logs. | Relative paths are resolved from the active config root. |
| `runtime.dedupe_store_path` | No | `data/processed_events.json` (relative) | Deduplication store for webhook event keys. | Relative paths are resolved from the active config root. |

### Tool loop budget

The three most important planning controls are:

- `tool_execution_max_rounds`: how many plan/replan cycles a request may use
- `tool_execution_max_steps`: how many steps the planner may propose in a single round
- `tool_execution_chunk_size`: how many of those steps are actually executed before the model replans

This keeps the architecture closer to a weak planner plus iterative tool loop rather than a single rigid multi-step plan.

---

## `logging`

| Field | Required | Default | Purpose | Notes |
| --- | --- | --- | --- | --- |
| `logging.level` | No | `DEBUG` | Application log verbosity. | Common values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `logging.file_path` | No | `logs/outline-agent.log` (relative) | Main service log file path. | Relative paths are resolved from the active config root. |

---

## Recommended starting point

For most users, the following is enough to start:

1. Set `outline.api_base_url`
2. Set `outline.api_key`
3. Set `outline.webhook_signing_secret`
4. Set one working model profile under `model_profiles`
5. Leave most feature flags enabled
6. Increase `model.timeout_seconds` if you expect long PDF or tool-heavy tasks

---

## Related files

- [`../README.md`](../README.md)
- [`../config.example.yaml`](../config.example.yaml)
- [`tools.md`](tools.md)
- [`data-layout.md`](data-layout.md)
