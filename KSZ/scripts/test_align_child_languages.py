import json
import stat
from types import SimpleNamespace

import pytest

import align_child_languages as script


@pytest.fixture
def database(monkeypatch):
    statements = []
    state = SimpleNamespace(conflict=False, statements=statements)

    class Database:
        process = SimpleNamespace(poll=lambda: None)

        def query(self, sql):
            statements.append(sql)
            if sql.startswith("SELECT JSON_OBJECT"):
                return [json.dumps({"id": str(i), "name": name, "before": old + "."})
                        for i, (name, (old, new)) in enumerate(script.REPLACEMENTS.items())]
            if sql.startswith("UPDATE"):
                assert "BINARY system_prompt=BINARY" in sql
                return ["0" if state.conflict else "1"]
            return []

        def close(self):
            pass

    monkeypatch.setattr(script, "Database", lambda container: Database())
    return state


def test_apply_creates_private_backup_before_committing(monkeypatch, tmp_path, database):
    backup = tmp_path / "before.json"
    monkeypatch.setattr("sys.argv", ["repair", "--apply", "--backup", str(backup)])
    script.main()
    rows = json.loads(backup.read_text())
    assert len(rows) == 3
    assert all(row["before"] != row["after"] for row in rows)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert database.statements[-1] == "COMMIT"


def test_concurrent_edit_rolls_back_whole_batch(monkeypatch, tmp_path, database):
    database.conflict = True
    monkeypatch.setattr("sys.argv", ["repair", "--apply", "--backup", str(tmp_path / "before.json")])
    with pytest.raises(RuntimeError, match="并发修改"):
        script.main()
    assert "COMMIT" not in database.statements
    assert database.statements[-1] == "ROLLBACK"


def test_existing_backup_is_preserved_without_updates(monkeypatch, tmp_path, database):
    backup = tmp_path / "before.json"
    backup.write_text("existing")
    monkeypatch.setattr("sys.argv", ["repair", "--apply", "--backup", str(backup)])
    with pytest.raises(FileExistsError):
        script.main()
    assert backup.read_text() == "existing"
    assert not any(sql.startswith("UPDATE") for sql in database.statements)
    assert database.statements[-1] == "ROLLBACK"


def test_preview_never_updates(monkeypatch, database):
    monkeypatch.setattr("sys.argv", ["repair"])
    script.main()
    assert not any(sql.startswith("UPDATE") for sql in database.statements)
    assert database.statements[-1] == "ROLLBACK"
