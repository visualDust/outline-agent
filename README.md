# Outline Agent

A small Python service for experimenting with an Outline agent driven by webhook-delivered comments.

Current MVP behavior:

- receives Outline webhooks at `/outline/webhook`
- verifies the `Outline-Signature` HMAC header
- focuses on `comments.create` events
- optionally limits handling to specific collection IDs
- defaults to **mention-first** triggering
- resolves LLM credentials/model from a repo-local YAML config
- pulls document text plus recent thread context before replying
- creates a temporary per-collection agent workspace with local memory files
- keeps per-thread session state inside each collection workspace
- automatically distills thread summaries / open questions back into `SESSION.md`
- reacts to accepted trigger comments with `👀` while processing, then swaps to `👍` when done
- can directly apply explicit user-requested Outline document edits via `documents.update`
- can use a thread-local `work/` sandbox for controlled file operations and short shell execution
- can upload generated local artifacts (for example PDFs) back to the current Outline document as attachments
- when artifacts are uploaded successfully, can also register clickable links to them inside the document body under an `Uploaded Artifacts` section for better in-document visibility
- can run a bounded multi-round local tool loop before replying when a task needs more than one tool pass
- can create and update a single in-thread progress/status comment during local tool execution, so long-running actions show `Working...` updates and a final action summary
- automatically splits overlong agent replies into numbered multiple Outline comments to stay under the API comment-length limit
- replies into the same comment thread via `comments.create`
- deduplicates already-processed comment events locally
- can write short durable memory updates back into the collection workspace `MEMORY.md`

## Why this repo exists

This is meant to be separate from `outline-skills`. `outline-skills` remains a general Outline API/CLI project; this repo is the webhook-driven agent prototype.

Recommended split of responsibilities:

- `outline-skills`: shared Outline capability layer (separate project)
- `outline-agent`: webhook runtime, prompt orchestration, collection-local memory, reply policy

In practice, if a new Outline API capability is missing, it should generally be added to `outline-skills` first, then consumed here as a library-facing capability.

## Configuration

The runtime loads configuration in this order:

1. CLI flags
2. environment variables
3. user config YAML at `~/.outline-agent/config.yaml`
4. built-in defaults

On first `outline-agent start`, if the config file does not exist, the CLI creates an initial
`config.yaml`, prints its path, and exits so the user can edit it before the service starts.

The YAML is grouped by module rather than keeping every setting at top level. The generated file
looks like this:

```yaml
server:
  host: 127.0.0.1
  port: 8787

outline:
  api_base_url: ""
  api_key: ""
  webhook_signing_secret: ""
  agent_user_id: ""

trigger:
  mode: mention
  mention_aliases:
    - "@agent"

model:
  ref: null

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

runtime:
  dry_run: false

logging:
  level: DEBUG
```

The app still accepts the flat environment variables shown in `.env.example` for runtime overrides,
but model provider profiles live in `config.yaml`.

Common config sections:

- `server`: host and port
- `outline`: API base URL, API key, webhook signing secret, agent user id
- `trigger`: trigger mode, mention aliases, collection allowlist
- `model`: runtime model refs and model client limits
- `model_profiles`: provider credentials and allowed model names
- `prompts`: prompt file overrides and prompt packs
- `features`: enable or disable memory/tool/document/thread/reaction related features
- `runtime`: workspace paths and `dry_run`
- `logging`: log level and log file path

Runtime data defaults live under `OUTLINE_AGENT_HOME`, not the current working directory. If you set
runtime path variables such as `WORKSPACE_ROOT` or `LOG_FILE_PATH` to relative paths, they are resolved
from the active config directory. By default that is `OUTLINE_AGENT_HOME`; with `--config-path`, it is
the passed config file's parent directory.

## Model configuration

Model provider configuration lives inside `config.yaml` under `model_profiles`.

Example:

```yaml
model_profiles:
  default: local-openai/gpt-5.4
  profiles:
    local-openai:
      provider: openai-responses
      base_url: https://your-gateway.example.com/openai/v1
      api_key: ...
      models:
        - gpt-5.4
```

Supported providers in this MVP:

- `openai-responses`
- `openai` / `openai-chat`
- `anthropic`

## Prompt files

Default prompts ship inside the Python package under `src/outline_agent/assets/prompts/`.

User overrides can live under:

```text
~/.outline-agent/
  prompts/
    00_system.md
    packs/
      outline_style.md
```

If no user override exists, the packaged prompt assets are used automatically.

## Related document retrieval

The processor can search the current collection for related documents and inject short excerpts into
reply and document-update prompts. Configure this with the `RELATED_DOCUMENT_*` settings in
environment variables or `config.yaml`.

## Collection workspaces

Each collection gets a local workspace under `WORKSPACE_ROOT`, for example:

```text
~/.outline-agent/data/agents/
  outline-agent-dev-sandbox-107b2669-e0ad-4abd-a66e-28305124edc8/
    memory/
      00_SYSTEM.md
      MEMORY.md
    scratch/
    threads/
      cad435c3-1cb9-4dd5-9254-d355b02fd795/
        SESSION.md
        PROMPT.md
        state.json
        work/
```

These files are bootstrapped automatically the first time the collection is seen.

- `00_SYSTEM.md`: collection-scoped agent instructions
- `MEMORY.md`: durable collection-specific memory / notes
- `scratch/`: collection-scoped temporary working files for future agent operations
- `threads/<root-comment-id>/SESSION.md`: thread-local summary, open questions, and working notes
- `threads/<root-comment-id>/PROMPT.md`: optional task-specific prompt instructions for the thread
- `threads/<root-comment-id>/state.json`: persisted recent turns, recent tool outcomes, progress-comment state, and thread metadata
- `threads/<root-comment-id>/work/`: thread-local working directory for controlled file edits and shell commands

Recommended mental model:

- **collection = agent workspace**
- **thread = session**

The current runtime still reconstructs document/comment context from Outline on each webhook,
but it now also persists per-thread state locally so follow-up replies can reuse recent turns,
an automatically maintained thread summary, and outstanding open questions across process restarts.

## Install

```bash
cd /home/zgong6/repos/outline-agent-comments
pip install -e .[dev]
pre-commit install
```

This repo uses `pre-commit` with Ruff only (`ruff check --fix` and `ruff format`).
There is no separate Black or isort hook.

## Run

```bash
cd /home/zgong6/repos/outline-agent-comments
outline-agent start
```

If `~/.outline-agent/config.yaml` does not exist yet, this command creates it and exits.

Without installing the package entry point, you can run the same startup path with:

```bash
python -m outline_agent start
```

Or directly with Uvicorn:

```bash
uvicorn outline_agent.app:app --host 127.0.0.1 --port 8787 --reload
```

For a small helper wrapper that respects `HOST`, `PORT`, and `RELOAD` environment overrides:

```bash
./scripts/run-dev.sh
```

`outline-agent start` does not enable reload unless you pass `--reload`.
`python -m outline_agent start` behaves the same as `outline-agent start`, but does not require
`pip install -e .`.
`./scripts/run-dev.sh` defaults to reload mode and always passes `--config-path ./config.yaml`.
If that file does not exist yet, the CLI creates it and exits. Override the path with
`CONFIG_PATH=/path/to/config.yaml ./scripts/run-dev.sh`.

## Endpoints

- `GET /healthz`
- `POST /outline/webhook`

## Logging

The service uses `loguru` for application logs.

By default it writes:

- stderr
- `~/.outline-agent/logs/outline-agent.log`

Log verbosity is controlled by `LOG_LEVEL`. File logs rotate at `10 MB` and keep the latest `5` files.

## Local data

The service writes runtime files under `~/.outline-agent/` by default:

- `data/webhooks/events.jsonl`
- `data/webhooks/last_event.json`
- `data/processed_events.json`
- `data/agents/...` collection workspaces
- `data/agents/.../threads/...` per-thread session state

## Notes / limitations

- Comment text extraction is intentionally lightweight and based on Outline's ProseMirror-style JSON payloads.
- Context gathering currently uses document text plus recent same-thread/relevant document comments.
- Agent replies are posted through Outline's `comments.create` markdown `text` field when possible, and retry with structured rich-text `data` if Outline returns certain 5xx / internal comment-create failures, so common formatting like paragraphs, lists, bold, italics, and inline code is more likely to survive fallback paths.
- Very long replies are automatically split into numbered thread replies (for example `1/3`, `2/3`, `3/3`) so each posted Outline comment stays within the API length limit, while trying to preserve Markdown block boundaries such as paragraphs, lists, and fenced code blocks.
- Durable memory write-back is intentionally conservative and appends only short deduplicated entries to `MEMORY.md`.
- Trigger detection currently supports real Outline user mentions via `OUTLINE_AGENT_USER_ID` plus text alias fallback.
- Thread-local session state is still lightweight compared with a full agent runtime, but it now stores recent turns plus an automatically maintained `SESSION.md` summary/open-question layer.
- Local tool use is intentionally conservative: it only operates inside `threads/<id>/work/`, supports a small built-in tool set, uses time-limited shell execution, can surface a single progress/status comment while actions are running, caps the per-comment tool loop to a bounded number of planning rounds, and stops repeated no-change / repeated-upload tool loops before they spin indefinitely.
- Direct document edits are intentionally conservative: the agent only applies them for explicit edit requests and may block edits when the provided document body is missing, truncated, or too ambiguous.
