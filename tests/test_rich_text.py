from __future__ import annotations

from outline_agent.utils.rich_text import (
    AttachmentRef,
    ImageRef,
    extract_attachment_refs,
    extract_image_refs,
    extract_plain_text,
    extract_prompt_text,
)

SAMPLE = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "mention", "attrs": {"label": "@agent"}},
                {"type": "text", "text": " there"},
            ],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Second line"},
            ],
        },
    ],
}


def test_extract_plain_text_from_prosemirror_doc() -> None:
    assert extract_plain_text(SAMPLE) == "Hello @agent there\nSecond line"


def test_extract_image_refs_and_prompt_text_include_embedded_images() -> None:
    sample = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Can you see this?"},
                    {"type": "image", "attrs": {"src": "/api/attachments.redirect?id=image-1", "alt": None}},
                ],
            }
        ],
    }

    images = extract_image_refs(sample)

    assert images == [ImageRef(src="/api/attachments.redirect?id=image-1", alt=None)]
    assert extract_prompt_text(sample) == "Can you see this?\n[attached image]"


def test_extract_attachment_refs_from_images_and_links() -> None:
    sample = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "image", "attrs": {"src": "/api/attachments.redirect?id=image-1", "alt": "chart"}},
                    {
                        "type": "text",
                        "text": "paper.pdf",
                        "marks": [
                            {"type": "link", "attrs": {"href": "attachments.redirect?id=paper-1"}}
                        ],
                    },
                ],
            }
        ],
    }

    refs = extract_attachment_refs(sample)

    assert refs == [
        AttachmentRef(source_url="/api/attachments.redirect?id=image-1", kind="image", label="chart"),
        AttachmentRef(source_url="/api/attachments.redirect?id=paper-1", kind="attachment", label=None),
    ]
