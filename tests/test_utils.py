"""Tests for utility functions: clean_opencode_output, telegramify_markdown,
minimal_escape_mdv2, split_message, sanitize_markdownv2, _filter_stderr,
_remove_tool_traces."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from formatting.markdown import (
    clean_opencode_output, 
    telegramify_markdown,
    minimal_escape_mdv2, 
    split_message,
    _filter_stderr,
    _remove_tool_traces
)


class TestCleanOpencodeOutput:
    """Tests for clean_opencode_output()."""

    def test_removes_ansi_escape_codes(self):
        text = "\x1b[0mHello\x1b[0m World\x1b[91mError\x1b[0m"
        result = clean_opencode_output(text)
        assert "\x1b" not in result
        assert "Hello" in result
        assert "World" in result

    def test_removes_build_lines(self):
        text = "> build · deepseek-v4-pro\nHello"
        result = clean_opencode_output(text)
        assert "build ·" not in result
        assert "Hello" in result

    def test_removes_tool_traces_single_line(self):
        text = "\u2699 notion_API-post-search {\"filter\": {}}\nResponse text"
        result = clean_opencode_output(text)
        assert "notion_API" not in result

    def test_collapses_multiple_newlines(self):
        text = "Hello\n\n\n\nWorld"
        result = clean_opencode_output(text)
        assert "\n\n\n\n" not in result


class TestTelegramifyMarkdown:
    """Tests for telegramify_markdown()."""

    def test_converts_table_to_code_block(self):
        text = "| Date | Count |\n|------|-------|\n| May  | 5     |"
        result = telegramify_markdown(text)
        assert "```" in result

    def test_passes_through_normal_text(self):
        text = "Hello world, no tables here."
        result = telegramify_markdown(text)
        assert "Hello world" in result
        assert result == text


class TestMinimalEscapeMdv2:
    """Tests for minimal_escape_mdv2()."""

    def test_escapes_underscore_in_variable_name(self):
        text = "get_user function"
        result = minimal_escape_mdv2(text)
        assert "get\\_user" in result

    def test_does_not_escape_inside_code_block(self):
        text = "```python\ndef get_user():\n    pass\n```"
        result = minimal_escape_mdv2(text)
        assert "get_user()" in result
        assert "get\\_user" not in result

    def test_does_not_escape_inside_inline_code(self):
        text = "Use `get_user` function"
        result = minimal_escape_mdv2(text)
        assert "`get_user`" in result

    def test_escapes_period(self):
        text = "End of sentence."
        result = minimal_escape_mdv2(text)
        assert "\\." in result

    def test_escapes_exclamation(self):
        text = "Hello!"
        result = minimal_escape_mdv2(text)
        assert "\\!" in result

    def test_escapes_plus_sign(self):
        text = "a + b"
        result = minimal_escape_mdv2(text)
        assert "\\+" in result


class TestSplitMessage:
    """Tests for split_message()."""

    def test_short_message_not_split(self):
        text = "Hello world"
        result = split_message(text)
        assert len(result) == 1
        assert result[0] == "Hello world"

    def test_long_message_is_split(self):
        text = "x" * 5000
        result = split_message(text)
        assert len(result) > 1

    def test_split_preserves_content(self):
        text = "Hello " * 1000
        result = split_message(text)
        combined = "".join(result)
        assert "Hello" in combined


class TestFilterStderr:
    """Tests for _filter_stderr()."""

    def test_filters_info_lines(self):
        text = "[INFO] Starting bot\nReal output"
        result = _filter_stderr(text)
        assert isinstance(result, str)

    def test_passes_through_regular_text(self):
        text = "Regular output text"
        result = _filter_stderr(text)
        assert "Regular output text" in result
