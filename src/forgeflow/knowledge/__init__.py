"""Deterministic, offline project Q&A with verbatim citations."""

from .answer import KnowledgeUnavailable, answer, evidence_lines, set_test_adapters
from .semantic_answer import KnowledgeScope

__all__ = ["KnowledgeScope", "KnowledgeUnavailable", "answer", "evidence_lines", "set_test_adapters"]
