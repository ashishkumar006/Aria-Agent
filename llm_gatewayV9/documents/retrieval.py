"""Retrieval-time document filtering.

Two things this module exists to get right:

* **Filtering happens AFTER the vector search.** Filtering the candidate set
  first would remove the disabled documents' vectors from the query itself,
  so an enabled document would lose good neighbours too.
* **An empty allowlist means "no documents", not "all documents".** The
  opposite convention is exactly how a disabled document keeps answering.

`doc_ids` is threaded through the plane to every drawer's `read`, and applied
to the merged result. Records without a `doc` span are ordinary memories and
are never touched by this filter.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def is_document_record(rec) -> bool:
    """True for chunks that came from an uploaded document."""
    doc = getattr(rec, "doc", None)
    return bool(doc is not None and getattr(doc, "doc_id", None))


def _doc_id(rec) -> str:
    return str(getattr(getattr(rec, "doc", None), "doc_id", "") or "")


def filter_by_documents(items, doc_ids):
    """Keep ordinary records always; keep document chunks only when their
    document is in `doc_ids`."""
    if doc_ids is None:
        return list(items)          # no filtering requested
    allow = set(doc_ids)
    out = []
    for r in items:
        if not is_document_record(r):
            out.append(r)
            continue
        if _doc_id(r) in allow:
            out.append(r)
    return out


@dataclass
class ConversationPreferences:
    """Per-conversation retrieval switches.

    Defaults are ON: once a document has been uploaded and enabled, chat and
    research may use it. A conversation that turns documents off excludes
    every document chunk from its prompt.
    """

    use_documents: bool = True
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"use_documents": self.use_documents, **self.extra}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ConversationPreferences":
        raw = raw if isinstance(raw, dict) else {}
        flag = raw.get("use_documents", True)
        # A malformed value must not silently turn documents OFF (which would
        # be surprising and hard to notice) - fall back to the default.
        use = flag if isinstance(flag, bool) else True
        extra = {k: v for k, v in raw.items() if k != "use_documents"}
        return cls(use_documents=use, extra=extra)