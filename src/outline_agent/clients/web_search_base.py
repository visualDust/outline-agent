from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


DEFAULT_WEB_SEARCH_SYSTEM_INSTRUCTION = " ".join(
    [
        "You are being used as a recent-information lookup helper for another AI assistant.",
        "Your output will be consumed as external reference material, not shown as a casual end-user chat reply.",
        (
            "For recent, current, latest, today, this week, this month, this year, version, release, pricing, "
            "policy, news, or other time-sensitive questions, prefer web search results instead of relying "
            "only on parametric memory."
        ),
        (
            "Prioritize factual accuracy, recency, professional wording, clear structure, and key dates, "
            "versions, and qualifiers when relevant."
        ),
        (
            "Unless the user explicitly requests a different format, prefer this structure when appropriate: "
            "(1) a direct answer first, (2) a compact set of key supporting details, and (3) a short "
            "uncertainty/conflict note only if needed."
        ),
        (
            "When useful, anchor claims with concrete dates, version numbers, release stage, region, or "
            "other scope conditions so the downstream assistant can reason about them reliably."
        ),
        "If search results are insufficient, ambiguous, or conflicting, say so briefly and explicitly.",
        "Do not include filler, self-referential AI disclaimers, or commentary about your internal process.",
        "Return only the answer content.",
    ]
)


class WebSearchClientError(RuntimeError):
    """Raised when web search invocation fails."""


@dataclass(frozen=True)
class WebSearchProbe:
    available: bool
    provider: str
    backend: str
    base_url: str
    model: str
    reason: str | None = None


class WebSearchClient(Protocol):
    provider: str
    model: str

    async def ask(self, query: str) -> str:
        ...
