"""Tests for corrupt sessions.json backup on load (roadmap item 4).

When the store cannot be parsed, load_session_map() must move the original
file aside as a timestamped `sessions.json.corrupt-*` sibling before returning
{}, so the data is preserved for forensic recovery instead of being lost.
"""

import persistence.sessions as sessions_mod
from persistence.sessions import load_session_map

CORRUPT = "{ this is not valid json"


def test_corrupt_sessions_json_is_backed_up(tmp_path, monkeypatch):
    db = tmp_path / "sessions.json"
    db.write_text(CORRUPT, encoding="utf-8")
    monkeypatch.setattr(sessions_mod, "SESSION_DB", db)

    result = load_session_map()

    assert result == {}
    backups = list(tmp_path.glob("sessions.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == CORRUPT
    assert not db.exists()
