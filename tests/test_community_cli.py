"""Local-only CLI coverage for NEWS and usefulness reporting."""

import json

import pytest

from meshgarden.database import connect, migrate
from meshgarden.main import main


@pytest.fixture
def cfgfile(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[radio]\n"
        'device = "/dev/fake"\n'
        'expected_node_id = "!11223344"\n'
        "[database]\n"
        f'path = "{tmp_path / "o.db"}"\n'
        "[backup]\n"
        f'directory = "{tmp_path / "backups"}"\n'
    )
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    conn.close()
    return str(config)


def test_news_cli_add_list_hide_and_usefulness(cfgfile, capsys):
    assert main(["--config", cfgfile, "news", "add", "meetup tonight"]) == 0
    assert "published" in capsys.readouterr().out

    assert main(["--config", cfgfile, "news", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    ident = listed[0]["id"]

    assert main(["--config", cfgfile, "news", "hide", str(ident)]) == 0
    assert "hidden" in capsys.readouterr().out
    assert main(["--config", cfgfile, "news", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert main(["--config", cfgfile, "usefulness"]) == 0
    assert json.loads(capsys.readouterr().out)["events"] == 0


def test_news_cli_delete_removes_the_row_and_reports_a_miss(tmp_path, cfgfile, capsys):
    """`hide` keeps the row (recoverable); `delete` removes it outright,
    so the text is gone from disk rather than merely un-shown, and a
    second delete of the same id exits 3 exactly as `hide` does for an
    id it cannot act on."""
    assert main(["--config", cfgfile, "news", "add", "published in error"]) == 0
    capsys.readouterr()
    assert main(["--config", cfgfile, "news", "list"]) == 0
    ident = json.loads(capsys.readouterr().out)[0]["id"]

    assert main(["--config", cfgfile, "news", "delete", str(ident)]) == 0
    assert "deleted" in capsys.readouterr().out

    conn = connect(str(tmp_path / "o.db"), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0
    finally:
        conn.close()

    assert main(["--config", cfgfile, "news", "delete", str(ident)]) == 3
    assert "not found" in capsys.readouterr().out
