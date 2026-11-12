"""Deciding the referent is different from asking about an attribute of it."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("review_grading", Path(__file__).parents[1] / "scripts/grade_review_model.py")
grading = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grading)


@pytest.mark.parametrize("text, expected", [
    ("Which of P00003 or P00006 has the larger quantity?", False),
    ("Cancelled supply. Which PO has the larger quantity?", False),
    ("Which cancelled order was cheaper?", False),
    ("取消通知涉及两单。请确认哪一单数量更大？", False),
    ("Which purchase commitment was cancelled: P00003 (price 16, 22 units) or P00006 (3 units)?", True),
    ("请确认取消的是哪一笔供货，采购单甲（原数量 22 件）还是乙（原数量 3 件）？", True),
    ("Can you confirm whether the supplier cancelled the component order or the finished-goods order?", True),
    ("We could cancel P00003; could you supply its price?", False),
])
def test_question_identifies_cancelled_commitment(text, expected):
    assert grading.cancellation_question(text) is expected
