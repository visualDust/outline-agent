# Tool and Capability Reference

This document lists the current built-in tool surface available to the agent.

## Tool summary

| Tool | Category | Side effect | Purpose | Typical use |
|---|---|---:|---|---|
| `get_current_document` | Outline document | Read | Load the active Outline document in the current thread | Summarize, inspect, or prepare a document action |
| `draft_new_document` | Outline drafting | Read | Draft a new document from the current request and context | Before `create_document` |
| `create_document` | Outline document | Write | Create a new Outline document | Create a separate summary, note, report, or derived doc |
| `draft_document_update` | Outline drafting | Read | Draft a safe update to the current Outline document | Before `apply_document_update` |
| `apply_document_update` | Outline document | Write | Apply a drafted update to the current Outline document | Edit/replace/extend the current doc |
| `ask_web_search` | External search | External | Ask the configured web search provider for fresh web information | Recent/current/web-dependent lookups |
| `list_dir` | Workspace | Read | Inspect files in the collection work directory | Discover files before reading/editing/running commands |
| `read_file` | Workspace | Read | Read a text file from the collection work directory | Inspect generated or downloaded files |
| `write_file` | Workspace | Write | Create or overwrite a workspace file | Generate temp files, scripts, markdown, HTML, etc. |
| `edit_file` | Workspace | Write | Replace one exact text occurrence in a file | Small surgical edits after reading a file |
| `run_shell` | Workspace/runtime | External | Run a focused shell command in the collection work dir | Rendering, conversion, inspection, simple local automation |
| `upload_attachment` | Outline attachment | Write | Upload a local file back to the current Outline document | Return PDFs, images, generated artifacts |
| `download_attachment` | Outline attachment | External | Download an Outline attachment or URL into the collection work dir | Read or transform user-provided files |
| `extract_text_from_txt` | Extraction | Read | Extract plain text from a text file | Use after download or file generation |
| `extract_text_from_md` | Extraction | Read | Extract normalized text from Markdown | Read markdown content for planning/summarization |
| `extract_text_from_csv` | Extraction | Read | Extract tabular CSV text | Read data-like attachments |
| `extract_text_from_pdf` | Extraction | Read | Best-effort visible text extraction from a PDF | Summarize or analyze a paper/report PDF |

## Capability groups

| Capability | Backing tools | Notes |
|---|---|---|
| Read current document | `get_current_document` | Core document-aware replies and edits |
| Draft and apply document edits | `draft_document_update`, `apply_document_update` | Draft first, then apply |
| Draft and create new documents | `draft_new_document`, `create_document` | Used for separate deliverables |
| Collection-local file workspace | `list_dir`, `read_file`, `write_file`, `edit_file` | Restricted to the collection `workspace/` dir |
| Focused shell execution | `run_shell` | Used for conversions/rendering when file ops alone are insufficient |
| Attachment round-trip | `download_attachment`, `upload_attachment` | Download user files, upload generated artifacts |
| Text extraction | `extract_text_from_txt`, `extract_text_from_md`, `extract_text_from_csv`, `extract_text_from_pdf` | Best-effort text ingestion for local files |
| Fresh web lookup | `ask_web_search` | Registered only when the selected web search provider is configured |

## Important behavior notes

- Document actions are planner-driven and bounded by configured round/step limits.
- `draft_document_update` and `draft_new_document` are read-only drafting steps; they do not modify Outline by themselves.
- `apply_document_update` only uses document fields such as `title`, `text`, and `content`.
- The local workspace is collection-scoped, so generated files survive individual thread deletion.
- Long-running tool work can surface progress updates through a single progress comment in the thread.
