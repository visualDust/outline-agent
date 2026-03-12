# Workspace, Memory, and Prompt Architecture

This document describes the current runtime storage model after the document-memory refactor.

## Executive summary

- There is **no thread `PROMPT.md` anymore**.
- There is **no thread `SESSION.md` anymore**.
- The only durable semantic memory layers are:
  - **collection memory**
  - **document memory**
- Thread storage now keeps:
  - transcript
  - runtime state
  - event log
- The only real file work area is the **collection workspace**.

## Runtime directory layout

For one collection, the workspace now looks like this:

```text
<workspace_root>/<collection_id>/
  memory/
    00_SYSTEM.md
    MEMORY.md
    index.json
  workspace/
  attachments/
  generated/
  scratch/
  documents/
    <document_id>/
      MEMORY.md
      state.json
  threads/
    <thread_id>/
      comments.json
      state.json
      events.jsonl
  archived_threads/
    <thread_id>/
      comments.json
      state.json
      events.jsonl
```

## Layer responsibilities

### 1. Collection layer

Path:

- `memory/00_SYSTEM.md`
- `memory/MEMORY.md`
- `workspace/`

Purpose:

- store collection-wide durable instructions and memory
- provide the only persistent work directory for generated files
- hold knowledge that should survive across documents and threads

Notes:

- `00_SYSTEM.md` is included into the final system prompt as collection-local instructions
- `MEMORY.md` stores collection facts, decisions, and working notes
- `workspace/` is where tools read/write files and where generated artifacts live before upload

### 2. Document layer

Path:

- `documents/<document_id>/MEMORY.md`
- `documents/<document_id>/state.json`

Purpose:

- store document-local durable memory
- keep reusable document summary / unresolved questions / working notes
- preserve context across multiple comment threads attached to the same document

Current semantics:

- this is the replacement for the old thread semantic session memory
- the agent reads this memory when replying, planning tools, deciding updates, and deciding routing

### 3. Thread layer

Path:

- `threads/<thread_id>/comments.json`
- `threads/<thread_id>/state.json`
- `threads/<thread_id>/events.jsonl`

Purpose:

- `comments.json`: source-of-truth transcript snapshot for this thread
- `state.json`: runtime/progress/tool-turn state
- `events.jsonl`: append-only debug/event log

Important:

- thread state is **not** the long-term semantic memory layer anymore
- thread deletion should not destroy collection/document memory

## Source of truth

### Transcript truth

The truth for thread content is the synced comment transcript:

- `threads/<thread_id>/comments.json`

It is rebuilt from Outline comments on webhook events so deletes and edits are reflected next time the agent runs.

### Semantic memory truth

- collection-wide memory truth: `memory/MEMORY.md`
- document-wide memory truth: `documents/<document_id>/MEMORY.md`

These are model-maintained summaries, not raw transcript mirrors.

### Runtime truth

- tool rounds
- progress comment bookkeeping
- recent turn metadata
- recent tool execution summaries

live in:

- `threads/<thread_id>/state.json`

## Event handling model

### `comments.create`

On a new comment:

1. collection workspace is ensured
2. document workspace is ensured
3. thread workspace is ensured
4. document comments are fetched
5. thread transcript is rebuilt/synced into `comments.json`
6. thread runtime state is refreshed from transcript
7. if trigger conditions pass, the agent is invoked

### `comments.update`

On comment edit:

1. the thread transcript is rebuilt
2. thread runtime state is refreshed
3. no reply is generated for the edit event itself
4. next invocation sees the updated transcript/state

### `comments.delete`

If the deleted comment is the thread root:

1. the thread is marked deleted
2. the thread workspace is archived into `archived_threads/`
3. the thread stops participating in active context lookup

If the deleted comment is a child comment:

1. the thread transcript is rebuilt without that child
2. thread runtime state is refreshed
3. future invocations see the cleaned transcript

## How the agent builds context

When the agent replies, the effective context is assembled from:

1. configurable base system prompt
2. configurable prompt packs
3. collection `00_SYSTEM.md`
4. collection `MEMORY.md`
5. document `MEMORY.md`
6. document `state.json`
7. thread `state.json`
8. thread comment transcript context
9. current document excerpt
10. current user comment

If a thread is long, thread comment context is truncated using:

- the first/root comment
- the latest tail comments
- a truncation marker
- guidance that exact omitted history can be retrieved with `get_thread_history`

## How memory is updated

### Collection memory

Managed by:

- `src/outline_agent/managers/memory_manager.py`
- `src/outline_agent/managers/memory_action_manager.py`

Two paths exist:

### A. Explicit memory actions

If the user explicitly asks to remember / forget / correct collection memory:

1. router enables memory action path
2. memory action manager proposes `add|update|delete|move`
3. `memory/MEMORY.md` is edited directly
4. `memory/index.json` is regenerated

This path is user-controlled and intentional.

### B. Automatic memory updates

After a successful reply:

1. the collection memory updater reviews the latest interaction
2. it proposes durable collection-level entries
3. `memory/MEMORY.md` is appended/updated
4. `memory/index.json` is regenerated

This should only retain reusable cross-document/cross-thread memory.

### Document memory

Managed by:

- `src/outline_agent/managers/document_memory_manager.py`

After a reply:

1. the manager sees current document memory
2. it sees the current document excerpt
3. it sees the latest user comment
4. it sees the assistant reply
5. it decides whether to rewrite:
   - `Summary`
   - `Open Questions`
   - `Working Notes`

This is the primary durable semantic memory layer for one document.

### Thread state

Managed mostly without semantic summarization:

- transcript sync rebuilds recent comment-derived state
- reply persistence records recent turns
- tool execution records recent tool runs
- progress comments update progress bookkeeping

This is operational state, not durable knowledge memory.

## Prompt surfaces: configurable vs hard-coded

### File-based configurable prompt surfaces

These are the main prompt sources intended for customization:

#### 1. Base system prompt

Files:

- `prompts/user/00_system.md`
- packaged default: `src/outline_agent/assets/prompts/user/00_system.md`

Purpose:

- global assistant behavior for comment replies

#### 2. Prompt packs

Files:

- `prompts/user/packs/*.md`
- packaged default pack: `src/outline_agent/assets/prompts/user/packs/outline_style.md`

Purpose:

- optional behavior/style overlays appended to the system prompt

#### 3. Collection-local system prompt

File:

- `memory/00_SYSTEM.md`

Purpose:

- per-collection local instruction layer
- generated from a code template initially, then persisted as a file

#### 4. Reply policy

Files:

- `prompts/user/reply_policy.md`
- packaged default: `src/outline_agent/assets/prompts/user/reply_policy.md`

Purpose:

- controls the final reply style guidance layered into the reply user prompt

### Hard-coded prompt surfaces

These prompts still have important hard-coded protocol/skeleton pieces in code.

| Location | Kind | Purpose |
|---|---|---|
| `src/outline_agent/processing/processor_prompting.py::build_user_prompt` | user prompt template | final reply prompt; injects document memory, thread runtime state, transcript context, doc excerpt, tool outcomes |
| `src/outline_agent/managers/action_router_manager.py::ACTION_ROUTER_SYSTEM_PROMPT` | protocol prompt | action-router protocol/schema; policy addendum now comes from `prompts/internal/action_router_policy.md` |
| `src/outline_agent/managers/memory_action_manager.py::MEMORY_ACTION_SYSTEM_PROMPT` | protocol prompt | memory-action protocol/schema; policy addendum now comes from `prompts/internal/memory_action_policy.md` |
| `src/outline_agent/managers/memory_manager.py::MEMORY_UPDATE_SYSTEM_PROMPT` | protocol prompt | collection memory updater protocol/schema; policy addendum now comes from `prompts/internal/memory_update_policy.md` |
| `src/outline_agent/managers/document_memory_manager.py::DOCUMENT_MEMORY_UPDATE_SYSTEM_PROMPT` | protocol prompt | document memory updater protocol/schema; policy addendum now comes from `prompts/internal/document_memory_update_policy.md` |
| `src/outline_agent/managers/document_creation_manager.py::DOCUMENT_CREATION_SYSTEM_PROMPT` | protocol prompt | document creation protocol/schema; policy addendum now comes from `prompts/internal/document_creation_policy.md` |
| `src/outline_agent/managers/document_creation_manager.py::_build_user_prompt` | user prompt template | provides current document/doc-memory/thread/comment context for new-document drafting |
| `src/outline_agent/managers/document_update_manager.py::DOCUMENT_UPDATE_SYSTEM_PROMPT` | protocol prompt | document update protocol/schema; policy addendum now comes from `prompts/internal/document_update_policy.md` |
| `src/outline_agent/managers/document_update_manager.py::_build_user_prompt` | user prompt template | provides outline/section context, doc memory, related docs, local file observations |
| `src/outline_agent/planning/tool_planner.py::UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT` | protocol prompt | tool planner protocol/schema; policy addendum now comes from `prompts/internal/tool_planner_policy.md` |
| `src/outline_agent/planning/tool_planner.py::_build_user_prompt` | user prompt template | provides tool catalog, work-dir inventory, prior rounds, document memory, attachments, and comment context |
| `src/outline_agent/state/workspace.py::INITIAL_SYSTEM_TEMPLATE` | initialization template | bootstrap template for collection `memory/00_SYSTEM.md` |
| `src/outline_agent/state/workspace.py::INITIAL_MEMORY_TEMPLATE` | initialization template | bootstrap template for collection `memory/MEMORY.md` |
| `src/outline_agent/state/workspace.py::INITIAL_DOCUMENT_MEMORY_TEMPLATE` | initialization template | bootstrap template for document `documents/<document_id>/MEMORY.md` |

### What is not currently externalized

Right now:

- `./prompts/user/` controls base reply behavior and style
- `./prompts/internal/` controls several internal policy addenda
- core protocol/schema text still lives in Python

The following are **not** yet externalized into prompt files:

- action router system prompt
- memory action system prompt
- final reply user-prompt template
- planner / updater / router user-prompt templates
- workspace bootstrap templates

So the current design is:

- **reply style / base assistant behavior**: file-configurable under `prompts/user/`
- **internal planner / updater heuristics**: file-configurable under `prompts/internal/`
- **protocol/schema and context assembly**: still hard-coded in Python

### Debugging notes

There are also a few smaller hard-coded prompt-ish helper strings outside the main table, for example:

- thread-context truncation guidance in `src/outline_agent/state/workspace.py`
- system-prompt wrapper labels such as `[Collection prompt]`
- fixed section labels like `Persisted document memory:` or `Relevant comment context:`

Those are not currently file-configurable either.

Useful files during debugging:

- collection memory: `memory/MEMORY.md`
- document memory: `documents/<document_id>/MEMORY.md`
- thread transcript: `threads/<thread_id>/comments.json`
- thread runtime state: `threads/<thread_id>/state.json`
- thread event log: `threads/<thread_id>/events.jsonl`

Recent logging improvements include debug-level logging around document memory proposal and application flow.
