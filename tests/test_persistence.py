"""Tests for load_session_map() and save_session_map()."""

import sys, os, json, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from persistence.sessions import load_session_map, save_session_map_atomic


class TestSessionPersistence:

    def test_load_nonexistent_file(self, monkeypatch):
        import persistence.sessions as sessions_mod
        original = sessions_mod.SESSION_DB
        from pathlib import Path
        sessions_mod.SESSION_DB = Path("/nonexistent/test_sessions.json")
        try:
            result = load_session_map()
            assert result == {}
        finally:
            sessions_mod.SESSION_DB = original

    def test_load_valid_json(self, tmp_path, monkeypatch):
        test_data = {"123": {"active": "default", "sessions": {}}}
        test_file = tmp_path / "sessions.json"
        test_file.write_text(json.dumps(test_data), encoding="utf-8")

        import persistence.sessions as sessions_mod
        original = sessions_mod.SESSION_DB
        sessions_mod.SESSION_DB = test_file
        try:
            result = load_session_map()
            assert result == test_data
        finally:
            sessions_mod.SESSION_DB = original

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        test_data = {"456": {"active": "test", "model": "deepseek/deepseek-v4-flash"}}
        test_file = tmp_path / "sessions.json"

        import persistence.sessions as sessions_mod
        original = sessions_mod.SESSION_DB
        sessions_mod.SESSION_DB = test_file
        try:
            asyncio.run(save_session_map_atomic(test_data))
            result = load_session_map()
            assert result == test_data
        finally:
            sessions_mod.SESSION_DB = original
