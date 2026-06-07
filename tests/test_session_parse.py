"""Tests for parse_opencode_session_list()."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ..persistence.sessions import parse_opencode_session_list


class TestParseOpencodeSessionList:

    def test_parses_valid_output(self):
        output = "ses_ABC123def456  My test session title  12:34"
        result = parse_opencode_session_list(output)
        assert len(result) == 1
        assert result[0]["id"] == "ses_ABC123def456"
        assert result[0]["title"] == "My test session title"
        assert "12:34" in result[0]["updated"]

    def test_parses_multiple_sessions(self):
        output = (
            "ses_AAA111  First session   10:00\n"
            "ses_BBB222  Second session  11:30 \u00b7 19/5/2026"
        )
        result = parse_opencode_session_list(output)
        assert len(result) == 2

    def test_handles_empty_output(self):
        result = parse_opencode_session_list("")
        assert len(result) == 0

    def test_handles_malformed_lines(self):
        output = "not a session line\nses_CCC333  Valid one  09:00"
        result = parse_opencode_session_list(output)
        assert len(result) == 1
        assert result[0]["id"] == "ses_CCC333"

    def test_parses_full_timestamp(self):
        output = "ses_DDD444  Test  22:00 \u00b7 19/5/2026"
        result = parse_opencode_session_list(output)
        assert "19/5/2026" in result[0]["updated"]
