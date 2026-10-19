"""WP3: operator-defined information commands (`[[commands.info]]`)."""

import random
from dataclasses import replace

import pytest

from meshgarden import modules
from meshgarden.commands import parse_command
from meshgarden.config import CommandsConfig, ConfigError, InfoCommand, parse_config
from meshgarden.main import Daemon
from tests.conftest import HAIKU_RNG_SEED, _cfg
from tests.fakes import FakeTransport, make_packet
from tests.test_daemon_meshcore import (
    VISITOR_PUBKEY,
    _start_meshcore_daemon,
)
from tests.test_daemon_meshcore import _cfg as _mc_cfg

ENTRY = {"name": "WIFI", "help": "Garden wifi info", "response": "SSID garden / pw sprout"}


def _data(*entries, lab="", **radio):
    return {
        "radio": {"device": "/dev/x", "expected_node_id": "!11223344", **radio},
        "database": {"path": "o.db"},
        "backup": {"directory": "b"},
        "service": {"lab_unlock_word": lab} if lab else {},
        "commands": {"info": list(entries)},
    }


def test_valid_entry_parses_and_normalizes_words():
    cfg = parse_config(_data({**ENTRY, "aliases": ["pw"], "scope": "channel"}))
    (e,) = cfg.commands.info
    assert (e.name, e.aliases, e.scope, e.enabled) == ("WIFI", ("PW",), "channel", True)


@pytest.mark.parametrize("word", ["MENU", "sos", "HELP", "FIND"])
def test_builtin_or_reserved_word_is_rejected(word):
    with pytest.raises(ConfigError, match=r"commands\.info\[1\].*reserved"):
        parse_config(_data({**ENTRY, "name": word}))


def test_alias_may_not_shadow_builtin():
    with pytest.raises(ConfigError, match="reserved"):
        parse_config(_data({**ENTRY, "aliases": ["MOON"]}))


def test_unlock_word_is_rejected_case_insensitively():
    with pytest.raises(ConfigError, match="lab_unlock_word"):
        parse_config(_data({**ENTRY, "name": "Sesame"}, lab="SESAME"))


def test_duplicates_across_entries_and_aliases_are_rejected():
    with pytest.raises(ConfigError, match="twice"):
        parse_config(_data(ENTRY, {**ENTRY, "name": "OTHER", "aliases": ["wifi"]}))


def test_oversize_response_and_help_are_rejected():
    with pytest.raises(ConfigError, match="response"):
        parse_config(_data({**ENTRY, "response": "x" * 181}))
    with pytest.raises(ConfigError, match="help"):
        parse_config(_data({**ENTRY, "help": "x" * 181}))


def test_response_is_checked_in_utf8_bytes():
    with pytest.raises(ConfigError, match="response"):
        parse_config(_data({**ENTRY, "response": "é" * 91}))  # 91 chars, 182 bytes


def test_meshcore_budget_override_tightens_the_limit():
    data = _data({**ENTRY, "response": "x" * 100}, transport="meshcore", meshcore_max_text_bytes=80)
    with pytest.raises(ConfigError, match="at most 80"):
        parse_config(data)


@pytest.mark.parametrize("bad", [{"response": ""}, {"scope": "everyone"}, {"bogus": 1},
                                 {"name": "bad word"}, {"enabled": "yes"}])
def test_other_invalid_entries_name_the_entry(bad):
    with pytest.raises(ConfigError, match=r"commands\.info\[1\]|word"):
        parse_config(_data({**ENTRY, **bad}))


def test_more_than_sixteen_entries_rejected():
    entries = [{**ENTRY, "name": f"CMD{i}"} for i in range(17)]
    with pytest.raises(ConfigError, match="at most 16"):
        parse_config(_data(*entries))


def _registry(tmp_path, *entries):
    cfg = replace(_cfg(tmp_path), commands=CommandsConfig(info=tuple(entries)))
    return modules.configured_registry(cfg)


def test_disabled_entries_are_absent_everywhere(tmp_path):
    reg = _registry(
        tmp_path,
        InfoCommand(name="WIFI", response="r", help="wifi help"),
        InfoCommand(name="OFF", response="r", help="off help", enabled=False),
    )
    more = reg.menu_more(lab_unlocked=False)
    assert "WIFI" in more and "OFF" not in more
    assert reg.help("WIFI", lab_unlocked=False).startswith("WIFI")
    assert reg.help("OFF", lab_unlocked=False) is None
    assert parse_command("OFF", modules=reg).module is None
    assert parse_command("WIFI", modules=reg).module == "WIFI"


def _daemon(tmp_path, *entries):
    cfg = replace(_cfg(tmp_path), commands=CommandsConfig(info=tuple(entries)))
    ft = FakeTransport(0x11223344, cfg.radio.expected_node_id, service_cfg=cfg.service)
    d = Daemon(cfg, transport=ft, start_metrics=False, haiku_rng=random.Random(HAIKU_RNG_SEED))
    d.iface = ft.iface
    d.start()
    return d


def test_dm_gets_the_literal_response_through_the_normal_path(tmp_path):
    d = _daemon(tmp_path, InfoCommand(name="WIFI", response="SSID {x} $HOME %s"))
    try:
        d.iface.emit_packet(make_packet(to_num=d.my_node_num, text="wifi"))
        d.drain()
        assert len(d.iface.sent) == 1
        text = d.iface.sent[0]["text"]
        # Literal (no templating); the request was counted against the daily quota.
        assert text.startswith("SSID {x} $HOME %s")
        used = d.conn.execute("SELECT SUM(commands) FROM node_daily_quota").fetchone()[0]
        assert used == 1
        row = d.conn.execute("SELECT command, state FROM processed_requests").fetchone()
        assert row["command"] == "CUSTOM"
    finally:
        d.stop()


@pytest.mark.parametrize("scope,expect_reply", [("dm", False), ("channel", True)])
def test_channel_answers_only_for_channel_scope(tmp_path, scope, expect_reply):
    entry = InfoCommand(name="WIFI", response="garden wifi", scope=scope)
    cfg = replace(
        _mc_cfg(tmp_path, mode="channel", mail_public=True),
        commands=CommandsConfig(info=(entry,)),
    )
    d, ft = _start_meshcore_daemon(cfg)
    try:
        ft.fake.emit_new_contact(VISITOR_PUBKEY, "visitor")
        ft.fake.emit_channel_message_from(0, "visitor", "WIFI")
        d.drain()
        texts = [m["msg"] for m in ft.fake.channel_sent]
        assert any("garden wifi" in t for t in texts) is expect_reply
        assert ft.fake.sent == []
    finally:
        d.stop()
