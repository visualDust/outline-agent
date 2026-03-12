Use `cross_thread_handoff = true` only when the user appears to refer to a different discussion thread in this same document.
Use `same_document_comment_lookup = true` only when the user asks to inspect, summarize, search, or compare other comments or threads in this same document.
If none are clearly needed, set all flags to false.
Be conservative: do not trigger special routes for ordinary task execution, normal follow-up replies, or simple document-edit requests.
