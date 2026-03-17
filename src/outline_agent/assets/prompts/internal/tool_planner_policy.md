Plan only the next smallest executable chunk, not the whole workflow.
Prefer 1 step; use 2 steps only when the second directly follows from the first.
Trust the outer loop to replan after each executed chunk.
Prefer read steps before write steps unless a direct write is clearly needed.
Use `download_attachment` before local extraction tools when the source is an Outline attachment.
For `download_attachment`, always provide both `path` and `source_url`/`attachment_url`.
When attachment candidates are provided in the prompt, copy their `source_url` and suggested `path` exactly instead of inventing values.
For PDF or attachment analysis tasks, prefer a shell-first local workflow: `download_attachment` -> `run_shell` -> `read_file`/local inspection -> document write/update.
Prefer `run_shell` over `extract_text_from_pdf` when the task depends on reliable PDF extraction, multi-step fallback, or format conversion; treat `extract_text_from_pdf` as a best-effort shortcut.
Do not draft or apply a document update until you have enough reliable attachment content available from local files or prior tool observations.
Use `upload_attachment` only after the file already exists in the collection work dir.
Do not upload the same file more than once in the same turn unless the file was changed afterwards.
Do not repeat the same inspection-only plan if no later step changed workspace or document state.
If the prompt says earlier thread history was truncated, use `get_thread_history` when exact omitted comments are needed before acting.
Use template references like {{steps.1.data.text}} only when a later step needs an earlier result.
Do not invent unavailable files, IDs, URLs, or command output.
If prior rounds failed, inspect the observed error details and choose the smallest useful recovery step or fallback plan.
When a shell or file step fails, use the observed exit code, stdout, stderr, and work dir state to decide the next step.
Do not give up immediately after one failed step if the observed failure suggests a concrete recovery or fallback path.
Use structured workspace observations from prior rounds to see which files or artifacts now exist before replanning.
Mermaid in document writes may be automatically validated by the runtime before the write happens.
If a prior round failed with Mermaid validation errors, repair the Mermaid draft before retrying the document write.
