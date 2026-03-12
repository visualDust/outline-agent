# Prompt Manifest

This document inventories the prompt surfaces in the project and classifies which ones are safe to customize.

## Risk model

### Level 1 — user-customizable

Safe to tune for style, tone, and presentation.

### Level 2 — maintainer-customizable

Safe for internal developers to tune carefully, but changes can alter planning, memory behavior, and routing quality.

### Level 3 — protocol-locked

Should remain in code. These prompts contain structured-output contracts, safety boundaries, or context-assembly skeletons.

## File-based prompts

### `prompts/user/00_system.md`

- Level: 1
- Purpose: base assistant behavior for comment replies
- Safe to change: tone, verbosity defaults, formatting preferences
- Unsafe to change: anything that encourages fabricated actions or ignores comment-rendering constraints

### `prompts/user/reply_policy.md`

- Level: 1
- Purpose: final reply-style instruction appended to the reply prompt
- Safe to change: brevity, acknowledgement style, clarification style
- Unsafe to change: anything that conflicts with runtime truth or encourages ignoring action outcomes

### `prompts/user/packs/*.md`

- Level: 1
- Purpose: optional style overlays
- Safe to change: structure, readability, language/style conventions

### `prompts/internal/tool_planner_policy.md`

- Level: 2
- Purpose: tool-planning heuristics layered on top of the fixed planner protocol
- Safe to change: planner conservatism, attachment workflow preference, recovery heuristics
- Risk: poor edits can make tool use inefficient or unstable

### `prompts/internal/document_update_policy.md`

- Level: 2
- Purpose: document-editing heuristics layered on top of the fixed document-update protocol
- Safe to change: preference for section edits vs broad rewrites, preservation bias
- Risk: over-aggressive edits or poor rewrite choices

### `prompts/internal/document_creation_policy.md`

- Level: 2
- Purpose: new-document drafting heuristics layered on top of the fixed creation protocol
- Safe to change: thresholds for when standalone docs are appropriate, desired draft quality
- Risk: over-creating or under-creating documents

### `prompts/internal/memory_update_policy.md`

- Level: 2
- Purpose: collection-memory retention heuristics layered on top of the fixed memory-update protocol
- Safe to change: what kinds of durable collection facts should be retained
- Risk: polluted or underpowered collection memory

### `prompts/internal/document_memory_update_policy.md`

- Level: 2
- Purpose: document-memory retention heuristics layered on top of the fixed document-memory protocol
- Safe to change: summary quality, open-question thresholds, note granularity
- Risk: transcript leakage into memory or loss of useful document-level continuity

### `prompts/internal/action_router_policy.md`

- Level: 2
- Purpose: conservative routing heuristics layered on top of the fixed action-router protocol
- Safe to change: how strict the router should be before enabling cross-thread lookup/handoff
- Risk: false-positive or false-negative special routing

### `prompts/internal/memory_action_policy.md`

- Level: 2
- Purpose: heuristics for explicit memory-management intent layered on top of the fixed memory-action protocol
- Safe to change: how conservative the system should be about interpreting “remember/forget/correct”
- Risk: accidental memory edits or missed user intent

## Protocol-locked prompt surfaces

These still live in Python and are intentionally not exposed as user-editable files:

- `processor_prompting.py::build_user_prompt`
  - final reply prompt context assembly
- `action_router_manager.py::ACTION_ROUTER_SYSTEM_PROMPT`
  - fixed router JSON contract
- `memory_action_manager.py::MEMORY_ACTION_SYSTEM_PROMPT`
  - fixed memory-action JSON contract
- `memory_manager.py::MEMORY_UPDATE_SYSTEM_PROMPT`
  - fixed collection-memory JSON contract
- `document_memory_manager.py::DOCUMENT_MEMORY_UPDATE_SYSTEM_PROMPT`
  - fixed document-memory JSON contract
- `document_creation_manager.py::DOCUMENT_CREATION_SYSTEM_PROMPT`
  - fixed document-creation JSON contract
- `document_update_manager.py::DOCUMENT_UPDATE_SYSTEM_PROMPT`
  - fixed document-update JSON contract
- `tool_planner.py::UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT`
  - fixed tool-planner JSON contract
- `*_build_user_prompt(...)`
  - runtime context skeletons for planners/updaters/routers
- `workspace.py` initialization templates
  - bootstrap templates for collection/document memory files

## Practical guidance

- If you want the agent to **sound different**, edit `prompts/user/`.
- If you want the agent to **plan/update/memorize differently**, edit `prompts/internal/` carefully.
- If you want to change **schemas, tool contracts, or routing contracts**, that is a code change, not a prompt-only change.
