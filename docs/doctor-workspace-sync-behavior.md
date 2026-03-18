# `outline-agent doctor workspace-sync` Behavior

This document describes the current behavior of the workspace-sync doctor command.

## Purpose

`outline-agent doctor workspace-sync` checks whether local agent workspace state still matches Outline.

It is meant for cases like:

- the agent was offline
- webhook delivery was missed
- local collection / document / thread state may now be stale

## Important Config Rule

The command only checks the workspace root from the active config / CLI overrides.

That means the result is only meaningful if `doctor` is pointed at the same config and workspace root that the agent used when it created local state.

Typical examples:

```bash
outline-agent doctor workspace-sync
```

or explicitly:

```bash
outline-agent doctor workspace-sync \
  --config-path /path/to/config.yaml \
  --workspace-root /path/to/agents
```

If `doctor` reports:

- `checked collections: 0`
- `checked documents: 0`
- `checked threads: 0`

the most common cause is that it is looking at the wrong `workspace_root`.

## Default Mode

Default depth is:

```bash
outline-agent doctor workspace-sync --depth coarse
```

Coarse mode checks:

- local collections whose remote collection is gone
- local documents whose remote document is deleted or gone
- active local threads that obviously hang off already-archived local documents
- missing local metadata files like `state.json` / `comments.json`

It does **not** do remote thread-root validation.

## Deep Mode

Deep mode adds thread-level checks:

```bash
outline-agent doctor workspace-sync --depth deep
```

Deep mode additionally checks:

- active thread whose remote root comment is gone
- active thread whose remote document comments cannot confirm the root
- deleted-but-still-active local thread workspaces

Deep mode groups thread checks by document and fetches comments once per document when possible.

## Current Remote Deletion Semantics

### Collections

A local collection is treated as deleted when:

- `collections.info(id)` returns not found

### Documents

A local document is treated as deleted when either of these is true:

- `documents.info(id)` returns not found
- `documents.info(id)` succeeds but `deletedAt != null`

This means documents in Outline Trash are currently treated as deleted for doctor purposes.

This is intentional: the command is trying to answer whether a local active workspace is still safe to trust as live state.

## 403 / Permission Errors

If Outline returns a permission-style error, doctor does **not** abort the whole scan.

Instead it reports findings like:

- `inaccessible_remote_collection`
- `inaccessible_remote_document`
- `inaccessible_remote_comments`

This means:

- the local workspace exists
- the current API key cannot confirm remote state for that object

These are warnings for operator review, not proof of deletion.

## `--fix` Behavior

By default, doctor is read-only:

```bash
outline-agent doctor workspace-sync
```

To archive local stale state:

```bash
outline-agent doctor workspace-sync --fix
```

The flow is:

1. run diagnostics
2. show findings
3. show a repair plan
4. ask for confirmation
5. apply local archival repairs
6. run post-fix verification

### What `--fix` will repair automatically

By default, `--fix` plans repairs for:

- missing remote collections
- deleted / missing remote documents
- orphaned active threads
- deleted-but-not-archived threads
- active threads whose parent is already archived locally

Repairs are local archival actions only:

- archive collection workspace
- archive document workspace
- archive thread workspace

It does **not** permanently delete local data.

### What `--fix` does with 403 findings

403/inaccessible findings are **not** included in the repair plan by default.

During interactive `--fix`, if inaccessible findings exist, doctor asks:

```text
Include N inaccessible (403) local workspaces in the repair plan? [y/N]
```

Default is `N`.

If the user answers yes, the inaccessible items are added to the repair plan and can then be archived locally.

## JSON Mode

For scripting:

```bash
outline-agent doctor workspace-sync --json
```

With `--fix --json`, the output includes the report, repair plan, repair run, and final verification report when applicable.

## Exit Codes

- `0`: no findings
- `1`: findings detected
- `2`: command execution or repair phase failed

## Typical Usage

Check only:

```bash
outline-agent doctor workspace-sync
```

Check with explicit config:

```bash
outline-agent doctor workspace-sync \
  --config-path ~/.outline-agent/config.yaml \
  --workspace-root .data/agents
```

Deep validation:

```bash
outline-agent doctor workspace-sync --depth deep
```

Interactive repair:

```bash
outline-agent doctor workspace-sync --fix
```

Non-interactive repair:

```bash
outline-agent doctor workspace-sync --fix --yes
```
