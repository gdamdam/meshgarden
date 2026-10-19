"""WP10: linking a MeshCore and a Meshtastic identity into one account."""

import json

import pytest

from meshgarden import accounts
from meshgarden.database import connect, migrate
from meshgarden.mail import Mail
from meshgarden.wordle import player_key

MT = "!11112222"
MC = "mc:!33334444"
MC2 = "mc:!55556666"
NOW = 1_800_000_000
DAY = "2027-01-15"


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "a.db"), bbs_path=str(tmp_path / "bbs.db"), transport="meshtastic")
    migrate(c, transport="meshtastic")
    yield c
    c.close()


def _link(conn, initiator=MT, joiner=MC, now=NOW):
    code = accounts.start(conn, initiator, now)
    return accounts.complete(conn, joiner, code, now + 5)


def test_code_is_single_use_expiring_and_cross_mesh_only(conn):
    code = accounts.start(conn, MT, NOW)
    with pytest.raises(accounts.LinkError) as exc:
        accounts.complete(conn, "!99998888", code, NOW + 1)
    assert exc.value.reason == "same_mesh"
    # Refused with a valid code: the code is burned.
    with pytest.raises(accounts.LinkError) as exc:
        accounts.complete(conn, MC, code, NOW + 2)
    assert exc.value.reason == "bad_code"
    code = accounts.start(conn, MT, NOW)
    with pytest.raises(accounts.LinkError):
        accounts.complete(conn, MC, code, NOW + accounts.CHALLENGE_SECONDS + 1)
    code = accounts.start(conn, MT, NOW)
    assert accounts.complete(conn, MC, code.lower(), NOW + 1) == MT
    assert accounts.canonical(conn, MC) == MT
    with pytest.raises(accounts.LinkError):
        accounts.complete(conn, MC2, code, NOW + 2)


def test_audit_holds_no_code(conn):
    code = accounts.start(conn, MT, NOW)
    accounts.complete(conn, MC, code, NOW + 1)
    dump = json.dumps([list(r) for r in conn.execute("SELECT * FROM identity_audit")])
    assert code not in dump
    assert code not in json.dumps([list(r) for r in conn.execute(
        "SELECT * FROM identity_challenges")])


def test_failed_attempts_are_capped(conn):
    accounts.start(conn, MT, NOW)
    for i in range(accounts.MAX_FAILED_PER_DAY):
        with pytest.raises(accounts.LinkError):
            accounts.complete(conn, MC, "ZZZZZZZZ", NOW + i)
    code = accounts.start(conn, MT, NOW + 10)
    with pytest.raises(accounts.LinkError) as exc:
        accounts.complete(conn, MC, code, NOW + 11)
    assert exc.value.reason == "too_many_attempts"


def test_blocked_identity_cannot_link_and_block_covers_account(conn):
    conn.execute("INSERT INTO blocked_senders(node_id, blocked_at) VALUES (?, 1)", (MC,))
    conn.commit()
    code = accounts.start(conn, MT, NOW)
    with pytest.raises(accounts.LinkError) as exc:
        accounts.complete(conn, MC, code, NOW + 1)
    assert exc.value.reason == "blocked"
    conn.execute("DELETE FROM blocked_senders")
    conn.commit()
    _link(conn)
    conn.execute("INSERT INTO blocked_senders(node_id, blocked_at) VALUES (?, 1)", (MC,))
    conn.commit()
    assert accounts.is_blocked_any(conn, MT)


def test_mail_and_reply_routes_follow_the_account(conn):
    conn.execute(
        "INSERT INTO mail(created_at, sender_node_id, recipient_node_id, message, "
        "message_hash) VALUES (?, '!77778888', ?, 'hi', 'h')", (NOW, MC))
    conn.execute("INSERT INTO mail_reply_routes VALUES (1, '!77778888', ?, ?)", (MC, NOW + 999))
    conn.commit()
    _link(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM mail WHERE recipient_node_id = ?", (MT,)).fetchone()[0] == 1
    assert Mail(conn).lookup_reply_route(1, MT, NOW) is not None
    assert Mail(conn).lookup_reply_route(1, MC, NOW) is None


def test_alias_prefs_and_lab_policy(conn):
    conn.executescript(f"""
        INSERT INTO mail_aliases VALUES ('{MT}', 'alice', 1);
        INSERT INTO mail_aliases VALUES ('{MC}', 'ally', 2);
        INSERT INTO mail_prefs VALUES ('{MC}', 1, 1);
        INSERT INTO lab_nodes VALUES ('{MC}', 1);
    """)
    _link(conn)
    assert [tuple(r) for r in conn.execute("SELECT node_id, alias FROM mail_aliases")] == [
        (MT, "alice")]
    assert conn.execute("SELECT enabled FROM mail_prefs WHERE node_id=?", (MT,)).fetchone()[0]
    assert conn.execute("SELECT 1 FROM lab_nodes WHERE node_id=?", (MT,)).fetchone()


def test_cursors_take_the_lower_mark(conn):
    conn.execute("INSERT INTO update_cursors VALUES (?, ?, 1)",
                 (MT, json.dumps({"guestbook": 50, "news": 9})))
    conn.execute("INSERT INTO update_cursors VALUES (?, ?, 1)",
                 (MC, json.dumps({"guestbook": 20})))
    conn.commit()
    _link(conn)
    snap = json.loads(conn.execute("SELECT snapshot FROM update_cursors").fetchone()[0])
    assert snap == {"guestbook": 20}


def test_board_cursors_take_the_lower_mark(conn):
    if not accounts._table_exists(conn, "board_cursors"):
        pytest.skip("board cursors not present")
    conn.executemany("INSERT INTO board_cursors(node_id, board, last_read_id, updated_at) "
                     "VALUES (?, ?, ?, 1)",
                     [(MT, "ham", 40), (MT, "wx", 10), (MC, "ham", 25)])
    conn.commit()
    _link(conn)
    rows = {tuple(r) for r in conn.execute(
        "SELECT node_id, board, last_read_id FROM board_cursors")}
    assert rows == {(MT, "ham", 25)}


def test_games_merge_without_extra_guesses_or_double_solve(conn):
    conn.executemany(
        "INSERT INTO wordle_state(player_key, day, guesses, solved, updated_at) VALUES (?,?,?,?,1)",
        [(player_key(MT, DAY, ""), DAY, 2, 0), (player_key(MC, DAY, ""), DAY, 4, 1)],
    )
    conn.execute("INSERT INTO wordle_daily(day, players, solves, total_guesses_on_solve) "
                 "VALUES (?, 2, 1, 4)", (DAY,))
    conn.commit()
    _link(conn)
    rows = conn.execute("SELECT player_key, guesses, solved FROM wordle_state").fetchall()
    assert [tuple(r) for r in rows] == [(player_key(MT, DAY, ""), 4, 1)]
    daily = conn.execute("SELECT players, solves FROM wordle_daily").fetchone()
    assert tuple(daily) == (1, 1)
    # Unlinking hands the leaving radio a copy of today's state, not a new game.
    assert accounts.unlink(conn, MC, NOW + 10, today=DAY) == MC
    state = conn.execute("SELECT guesses, solved FROM wordle_state WHERE player_key = ?",
                         (player_key(MC, DAY, ""),)).fetchone()
    assert tuple(state) == (4, 1)
    assert tuple(conn.execute("SELECT players, solves FROM wordle_daily").fetchone()) == (1, 1)


def test_unlink_keeps_mail_with_canonical_and_is_audited(conn):
    _link(conn)
    assert accounts.unlink(conn, MT, NOW + 10) == MC
    assert accounts.canonical(conn, MC) == MC
    assert accounts.unlink(conn, MT, NOW + 11) is None
    events = [r[0] for r in conn.execute("SELECT event FROM identity_audit ORDER BY id")]
    assert events == ["link_started", "linked", "unlinked"]


def test_unlinked_identities_unchanged(conn):
    assert accounts.canonical(conn, MT) == MT
    assert accounts.members(conn, MC) == (MC,)


# --- daemon integration: the Meshtastic side of a link ---------------------


@pytest.fixture
def linking_daemon(tmp_path):
    from dataclasses import replace

    from meshgarden.main import Daemon
    from tests.conftest import _cfg, _unlock_standard_lab_senders
    from tests.fakes import FakeTransport

    cfg = _cfg(tmp_path)
    cfg = replace(
        cfg,
        database=replace(cfg.database, bbs_path=str(tmp_path / "bbs.db")),
        service=replace(cfg.service, identity_linking=True),
    )
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False)
    d.iface = ft.iface
    d.start()
    _unlock_standard_lab_senders(d.conn)
    yield d
    d.stop()


def _dm(daemon, text, packet_id, from_num=0xAABBCCDD):
    from tests.fakes import make_packet

    daemon.iface.emit_packet(make_packet(
        to_num=daemon.my_node_num, from_num=from_num, text=text, packet_id=packet_id))
    daemon.drain()
    daemon.advance_seconds(60)  # past the per-node command cooldown
    return daemon.iface.sent[-1]["text"]


def test_daemon_link_shares_mailbox_and_reply_routes(linking_daemon):
    from meshgarden.mail import MailPrefs

    d = linking_daemon
    mc = "mc:!12345678"
    # The MeshCore daemon issued this code to its radio (same shared file).
    code = accounts.start(d.conn, mc, NOW)
    MailPrefs(d.conn).set(mc, True, 1000)
    Mail(d.conn).send("!0badf00d", mc, "for the meshcore radio", 1000)
    assert "Linked" in _dm(d, f"LINK {code}", 9601)
    assert accounts.canonical(d.conn, "!aabbccdd") == mc
    # The Meshtastic radio now reads the account's inbox.
    assert "for the meshcore radio" in _dm(d, "MAIL", 9602)
    # Mail addressed to the Meshtastic radio lands in the same inbox.
    _dm(d, "MAIL mc:!12345678 note to self", 9603, from_num=0x0BADF00D)
    assert {r[0] for r in d.conn.execute("SELECT recipient_node_id FROM mail")} == {mc}
    # Linking twice or from a third radio is refused.
    assert "Already linked" in _dm(d, "LINK", 9604)


def test_daemon_link_bad_code_and_unlink(linking_daemon):
    d = linking_daemon
    assert "failed" in _dm(d, "LINK ABCDEFGH", 9611)
    code = accounts.start(d.conn, "mc:!12345678", NOW)
    _dm(d, f"LINK {code}", 9612)
    assert "own empty mailbox" in _dm(d, "UNLINK", 9613)
    assert accounts.canonical(d.conn, "!aabbccdd") == "!aabbccdd"
    assert "not linked" in _dm(d, "UNLINK", 9614)


def test_linking_off_keeps_link_unknown(tmp_path):
    from meshgarden.commands import Command, parse_command
    from meshgarden.modules import configured_registry
    from tests.conftest import _cfg

    reg = configured_registry(_cfg(tmp_path))
    assert parse_command("LINK", modules=reg).command is Command.UNKNOWN


def test_pending_cursor_of_linked_radio_settles_into_account(conn):
    """Integration (identity x unread): the ack is in the asking radio's
    request log, the cursor advance goes to the account."""
    from meshgarden import board_unread, updates

    _link(conn)  # account MT, joiner MC
    updates.remember(conn, MC, 77, {"guestbook": 5}, NOW)
    board_unread.remember(conn, MC, 77, "ham", 9, NOW)
    conn.execute(
        "INSERT INTO processed_requests(sender_node_id, packet_id, first_seen_at, expires_at, "
        "command, state, updated_at) VALUES ('!33334444', 77, ?, ?, 'NEW', 'acked', ?)",
        (NOW, NOW + 999, NOW))
    conn.commit()

    def local(stored):
        return stored.removeprefix("mc:") if stored.startswith("mc:") else None

    for mod in (updates, board_unread):
        mod.settle(conn, NOW + 1, 600, local_id=local,
                   cursor_id=lambda s: accounts.canonical(conn, s))
    snap = conn.execute("SELECT snapshot FROM update_cursors WHERE node_id = ?", (MT,)).fetchone()
    assert json.loads(snap[0]) == {"guestbook": 5}
    assert conn.execute("SELECT last_read_id FROM board_cursors WHERE node_id = ? AND board='ham'",
                        (MT,)).fetchone()[0] == 9


def test_operator_cli_status_and_unlink(tmp_path, capsys):
    from dataclasses import replace

    from meshgarden.main import cmd_identity
    from tests.conftest import _cfg

    cfg = _cfg(tmp_path)
    cfg = replace(cfg, database=replace(cfg.database, bbs_path=str(tmp_path / "bbs.db")))
    c = connect(cfg.database.path, bbs_path=cfg.database.bbs_path, transport="meshtastic")
    migrate(c, transport="meshtastic")
    code = accounts.start(c, MT, NOW)
    accounts.complete(c, MC, code, NOW + 1)
    c.close()
    assert cmd_identity(cfg, "status", MT) == 0
    out = capsys.readouterr().out
    assert "linked" in out and code not in out
    assert cmd_identity(cfg, "unlink", MC) == 0
    assert cmd_identity(cfg, "unlink", MC) == 3
    assert cmd_identity(cfg, "status", "nonsense") == 2
