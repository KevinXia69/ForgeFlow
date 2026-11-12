from forgeflow.api.app import _format_lock


def test_table_lock_wins_over_cell_sentence_count() -> None:
    lock = _format_lock("用表格列三列：现在能做、现在不能做、对应证据。每格一句话。")
    assert "Markdown table" in lock
    assert "one sentence" not in lock


def test_english_request_is_explicit() -> None:
    assert "entirely in English" in _format_lock("请用英文回答：它能替代计划员吗？")


def test_character_limit_covers_the_whole_answer() -> None:
    lock = _format_lock("给客户写一封120字以内的邮件：断供后你们能做什么？")
    assert "at most 120 characters" in lock
    assert "greeting" in lock


def test_about_characters_has_a_floor_and_ceiling() -> None:
    lock = _format_lock("写一段60秒口播稿，约150字。")
    assert "about 150 characters" in lock
    assert "120" in lock and "180" in lock


def test_chinese_point_count_is_converted() -> None:
    assert "exactly 5 numbered items" in _format_lock("用五条各一句列出这套系统的价值。")


def test_a_plain_question_has_no_format_lock() -> None:
    assert _format_lock("介绍一下 ForgeFlow 是什么。") == ""


def test_sentence_count_is_converted_and_explicit() -> None:
    assert "exactly 3 sentences" in _format_lock("用三句话说明它是什么、谁用、解决什么问题。")
    assert "exactly 3 sentences" in _format_lock("In three sentences, explain the product.")
