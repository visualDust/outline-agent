# `.data` Directory Layout

This document describes the **currently observed** runtime data layout under `.data/` and the role of each major directory and file.

> Notes
>
> - This description is based on the current repository state and current runtime artifacts, not on older documentation or legacy assumptions.
> - `.data/` is a **runtime artifact directory**. Its actual root location may vary by deployment or configuration; in this repository it is currently located under the project root.
> - Some files and subdirectories are **created on demand** and only appear when the corresponding feature is used.

---

## 1. Top-level layout

The current top-level `.data/` layout looks like this:

```text
.data/
├── agents/
├── logs/
├── webhooks/
├── processed_events.json
└── test-service.log
```

| Path | Purpose |
| --- | --- |
| `.data/agents/` | Main agent workspace, organized by Outline collection. Each collection workspace stores memory, document state, thread state, and file workspaces. |
| `.data/logs/` | Service log output directory. The current instance includes both the main service log and comment-related logs. |
| `.data/webhooks/` | Raw webhook capture data used for debugging, inspection, and replay-style analysis. |
| `.data/processed_events.json` | Event deduplication state used to avoid handling the same webhook event multiple times. |
| `.data/test-service.log` | Extra local test or experiment log. This is not part of the core workspace protocol and appears to be instance-specific. |

---

## 2. `agents/`: collection-scoped primary workspaces

Each first-level directory under `agents/` corresponds to a collection-scoped workspace.

Directory names are typically one of the following:

- a readable slug plus the collection ID
- a sanitized form of the collection ID

Examples currently present in the repository include:

- `paper-reading-ecaa3b5c-0e6d-4540-b056-2bef754b85c9`
- `ics-2026-camera-ready-preparation-1b38abee-08f1-4790-a6f3-7c2dd1726d08`
- `ing-bd58ac0d-3322-4977-a232-346be2e4a6a0`

A collection workspace currently looks like this:

```text
.data/agents/<collection-workspace>/
├── archived_threads/
├── documents/
├── memory/
├── scratch/
├── threads/
└── workspace/
```

### 2.1 `memory/`

This directory stores collection-level durable memory and prompt entry files.

Typical contents:

```text
memory/
├── 00_SYSTEM.md
├── MEMORY.md
└── index.json        # optional
```

| File | Purpose |
| --- | --- |
| `00_SYSTEM.md` | Persisted collection-level system prompt template. |
| `MEMORY.md` | Durable collection-level memory, readable by both humans and the agent. |
| `index.json` | Structured index or derived representation of collection memory. It currently appears only in some workspaces, so it should be treated as optional and feature-dependent. |

### 2.2 `documents/`

Each Outline document gets its own subdirectory for **document-level state** and **document-level memory**.

Example:

```text
documents/
└── <document-id>/
    ├── MEMORY.md
    └── state.json
```

| File | Purpose |
| --- | --- |
| `MEMORY.md` | Durable document-local memory. This is typically used for persistent judgments, constraints, or notes specific to that document. |
| `state.json` | Minimal document state. In the currently observed layout it mainly contains metadata such as `document_id` and `document_title`. |

### 2.3 `threads/`

Each comment thread gets its own subdirectory. This is the core storage area for the current session or conversation state.

Example:

```text
threads/
└── <thread-id>/
    ├── comments.json
    ├── events.jsonl
    └── state.json
```

| File | Purpose |
| --- | --- |
| `comments.json` | Thread transcript snapshot, including comment tree structure plus rich-text and plain-text content. This is useful for restoring thread-level context. |
| `events.jsonl` | Incremental event stream for the thread. Each line is a JSON event, making it useful for execution tracing and debugging. |
| `state.json` | Aggregated thread state, including recent comments, participants, progress-comment state, tool-run state, and turn counters. |

Based on the current runtime samples, `threads/*/state.json` is a central runtime index. It records fields such as:

- `last_comment_*`
- `interaction_count`
- `comment_count`
- `assistant_turn_count`
- `participants`
- `recent_comments`
- progress and tool-run related state

### 2.4 `archived_threads/`

This directory stores archived thread workspaces.

Its purpose is to separate thread lifecycle states:

- active threads remain under `threads/`
- deleted, archived, or otherwise moved threads are placed under `archived_threads/`

In the current repository snapshot, many of these directories are empty, so this should be understood as a lifecycle-management area rather than a directory that always contains data.

### 2.5 `workspace/`

This is the collection-level shared file workspace used by tools, local processing steps, and attachment handling.

Currently observed structure:

```text
workspace/
├── attachments/
│   └── document/     # common but created on demand
├── comment_images/   # created on demand
└── generated/
```

| Subdirectory | Purpose |
| --- | --- |
| `attachments/` | Downloaded attachments and locally cached source files. Common for PDFs, images, and document attachment analysis. |
| `attachments/document/` | A common location for document-scoped attachments in the current implementation, for example when a PDF attached to a document is downloaded for analysis. |
| `comment_images/` | Images extracted from comments and downloaded locally. This appears only when image-based comment workflows are used. |
| `generated/` | Generated intermediate files and derived outputs produced by local tools, such as extracted text files. |

### 2.6 `scratch/`

This is a reserved temporary workspace.

Characteristics of `scratch/` in the current layout:

- it commonly exists
- it may or may not contain files at a given time
- it behaves more like a general-purpose temporary buffer than a directory with strict protocol-level semantics

---

## 3. Expanded example of a collection workspace

Using the currently observed `paper-reading-...` workspace as an example, the structure can be summarized like this:

```text
.data/agents/paper-reading-<collection-id>/
├── archived_threads/
├── documents/
│   ├── <document-id>/
│   │   ├── MEMORY.md
│   │   └── state.json
│   └── ...
├── memory/
│   ├── 00_SYSTEM.md
│   ├── MEMORY.md
│   └── index.json
├── scratch/
├── threads/
│   ├── <thread-id>/
│   │   ├── comments.json
│   │   ├── events.jsonl
│   │   └── state.json
│   └── ...
└── workspace/
    ├── attachments/
    │   └── document/
    ├── comment_images/
    └── generated/
```

---

## 4. Top-level data outside `agents/`

### 4.1 `logs/`

Currently observed:

```text
.data/logs/
├── outline-agent-comments.log
└── outline-agent.log
```

These files are instance-level runtime logs rather than conversation state, but they are important for debugging and operational diagnosis.

### 4.2 `webhooks/`

Currently observed:

```text
.data/webhooks/
├── events.jsonl
└── last_event.json
```

| File | Purpose |
| --- | --- |
| `events.jsonl` | Archived raw webhook event stream for long-term debugging and inspection. |
| `last_event.json` | Full snapshot of the most recently received webhook event, useful for quickly checking what was just delivered. |

### 4.3 `processed_events.json`

This file stores event deduplication state.

Current format:

```json
{
  "keys": ["comments.create:...", "comments.create:..."]
}
```

Its purpose is to prevent duplicate handling when Outline retries webhook delivery or when the same event is received more than once.

---

## 5. Architectural layering reflected in the layout

The current `.data/` structure reflects four distinct layers:

1. **Instance-level data**
   - `logs/`
   - `webhooks/`
   - `processed_events.json`

2. **Collection-level data**
   - `agents/<collection>/memory/`
   - `agents/<collection>/workspace/`
   - `agents/<collection>/scratch/`

3. **Document-level data**
   - `agents/<collection>/documents/<document-id>/...`

4. **Thread-level data**
   - `agents/<collection>/threads/<thread-id>/...`

In other words, this is not a flat state directory. The runtime data is explicitly separated by:

- collection
- document
- thread
- runtime instance

---

## 6. Core runtime structure vs. auxiliary artifacts

### Core runtime structure

The following paths can be treated as the primary skeleton of the current implementation:

- `.data/agents/<collection>/memory/`
- `.data/agents/<collection>/documents/<document-id>/state.json`
- `.data/agents/<collection>/threads/<thread-id>/comments.json`
- `.data/agents/<collection>/threads/<thread-id>/events.jsonl`
- `.data/agents/<collection>/threads/<thread-id>/state.json`
- `.data/processed_events.json`
- `.data/webhooks/events.jsonl`
- `.data/webhooks/last_event.json`

### Optional or auxiliary artifacts

The following are better understood as feature-specific or instance-specific artifacts:

- `memory/index.json`
- `workspace/comment_images/`
- `workspace/generated/`
- `workspace/attachments/document/`
- `.data/test-service.log`
- `archived_threads/` (important as a lifecycle concept, but not always populated)

---

## 7. Most important mental model for maintainers

If you remember only one thing about `.data/`, it should be this:

> **`threads/` stores conversation process state, `documents/` stores document-local durable state, `memory/` stores collection-level durable memory, and `workspace/` stores local files and tool-generated artifacts.**

Put differently:

- For **conversation history and thread runtime state**, look under `threads/`
- For **document-local memory and metadata**, look under `documents/`
- For **collection-level long-term context**, look under `memory/`
- For **attachments, extracted text, and generated files**, look under `workspace/`
- For **instance-level logging and webhook debugging**, look under `.data/logs/` and `.data/webhooks/`
