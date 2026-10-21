"""Deterministic, offline project Q&A with verbatim citations."""

from .answer import answer, evidence_lines
from .index import DocumentIndex, Passage

__all__ = ["DocumentIndex", "Passage", "answer", "evidence_lines"]
