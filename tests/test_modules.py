"""WP9: the optional command-module interface."""

import pathlib

import pytest

from meshgarden import modules
from meshgarden.commands import Command, parse_command
from meshgarden.modules import CommandModule, ModuleError, ModuleRegistry, ModuleRequest
from tests.conftest import _cfg


def _req(**kw):
    base = dict(argument="", sender="!aabbccdd", now=0, max_bytes=180, channel=False,
                lab_unlocked=False)
    base.update(kw)
    return ModuleRequest(**base)


def _mod(name="NET", **kw):
    kw.setdefault("handler", lambda req: "net body")
    return CommandModule(name=name, command=Command.CUSTOM, usage=f"{name}\nusage", **kw)


def test_moon_and_sun_are_builtin_modules_with_unchanged_parsing():
    assert parse_command("MOON").command is Command.MOON
    assert parse_command("!sun").command is Command.SUN
    assert parse_command("MOON").module == "MOON"
    assert parse_command("MOON tonight").command is Command.UNKNOWN
    assert parse_command("HELP MOON").command is Command.CMD_HELP


def test_registry_resolves_words_and_aliases():
    reg = modules.build_registry([_mod(aliases=("CLUBNET",), menu="NET club net")])
    parsed = parse_command("clubnet", modules=reg)
    assert (parsed.command, parsed.module) == (Command.CUSTOM, "NET")
    assert parse_command("NET extra", modules=reg).command is Command.UNKNOWN
    assert parse_command("HELP NET", modules=reg).command is Command.CMD_HELP
    assert reg.menu_more(lab_unlocked=False) == "➕ More\nNET club net"


def test_operator_module_cannot_shadow_builtin_or_reserved_words():
    for word in ("MENU", "SOS", "HELP", "MAIL", "MOON", "!GARDEN"):
        with pytest.raises(ModuleError):
            ModuleRegistry([_mod(word, operator=True)])
    with pytest.raises(ModuleError):
        ModuleRegistry([_mod("NET"), _mod("NET")])
    with pytest.raises(ModuleError):
        ModuleRegistry([_mod("NOT A WORD")])


def test_builtins_still_win_over_a_module_word():
    # A non-operator module that reused a built-in word would never be reached.
    reg = modules.build_registry([_mod("STATS")])
    assert parse_command("STATS", modules=reg).command is Command.STATS


def test_failing_handler_is_isolated():
    def boom(req):
        raise RuntimeError("provider down")

    reg = modules.build_registry([_mod(handler=boom)])
    assert reg.run(reg.lookup("NET"), _req()) == "NET unavailable right now."
    assert reg.run(reg.lookup("MOON"), _req()).strip()


def test_lab_modules_hidden_until_unlocked():
    reg = modules.build_registry([_mod(lab=True, menu="NET lab net")])
    assert reg.help("NET", lab_unlocked=False) is None
    assert reg.help("NET", lab_unlocked=True) == "NET\nusage"
    assert reg.menu_more(lab_unlocked=False) is None
    assert reg.lab_words() == {"NET"}


def test_broken_factory_is_skipped(monkeypatch, tmp_path):
    def broken(cfg):
        raise RuntimeError("bad")

    monkeypatch.setattr(modules, "optional_factories", lambda: [broken])
    reg = modules.configured_registry(_cfg(tmp_path))
    assert reg.lookup("MOON") is not None


def test_module_sources_never_name_send_methods():
    root = pathlib.Path(modules.__file__).parent
    for path in [root / "modules.py", *root.glob("mod_*.py")]:
        text = path.read_text()
        for forbidden in ("send_dm", "send_channel", "send_text", "Sender(", "_reserve_and_send"):
            assert forbidden not in text, (path.name, forbidden)
