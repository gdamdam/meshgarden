"""Optional command modules: small, bundled commands behind one interface.

A module only turns a request into a reply body. Central routing in
main.py keeps everything else: eligibility, LAB and channel gates, block
list, rate limits, dedup, quotas and the single audited Sender path. A
module is handed no transport and no send callable, so it cannot transmit;
`tests/test_modules.py` pins that no module file names a send method.

Modules are bundled code chosen by config. There is no loading from paths,
entry points or the network.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from meshgarden.commands import RESERVED_COMMAND_WORDS, Command

log = logging.getLogger(__name__)

# Command words are a single printable ASCII token, like the built-ins.
_MAX_WORD_LEN = 12


@dataclass(frozen=True)
class ModuleRequest:
    """What a handler may see. `sender` is the BBS-qualified node id the
    radio authenticated, never a display name."""

    argument: str
    sender: str
    now: int
    max_bytes: int
    channel: bool
    lab_unlocked: bool
    conn: sqlite3.Connection | None = None
    # The radio's own stored BBS id, before any account linking maps it to
    # an account id (`sender`). Only LINK/UNLINK need the difference.
    identity: str = ""


@dataclass(frozen=True)
class CommandModule:
    name: str
    command: Command  # processed_requests / metrics label
    usage: str  # `HELP <word>` reply, <= 180 bytes
    handler: Callable[[ModuleRequest], str]
    aliases: tuple[str, ...] = ()
    menu: str = ""  # MENU MORE entry; "" keeps the command unlisted
    channel_ok: bool = False  # may answer on the MeshCore public channel
    lab: bool = False  # gated behind the LAB unlock like POST/READ
    takes_argument: bool = False
    builtin: bool = False  # extracted core command; listed on its own menu pages
    operator: bool = False  # defined in config, so it may not take a reserved word
    # Optional background service (start()/stop()), e.g. a provider fetcher.
    # Built only by an enabled factory; the daemon starts it after startup
    # and stops it at shutdown. Handlers read its results, never wait on it.
    service: Any = None

    @property
    def words(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class ModuleError(ValueError):
    """A module definition collides with a built-in or another module."""


def validate_word(word: str, *, what: str) -> str:
    upper = word.strip().upper()
    # A leading "!" is the bot-style spelling some built-ins accept (!MOON).
    body = upper.removeprefix("!")
    if not body or len(upper) > _MAX_WORD_LEN or not upper.isascii() or not all(
        c.isalnum() or c in "-_" for c in body
    ):
        raise ModuleError(f"{what} {word!r} must be 1-{_MAX_WORD_LEN} ASCII letters/digits")
    return upper


@dataclass
class ModuleRegistry:
    modules: list[CommandModule] = field(default_factory=list)
    reserved: frozenset[str] = RESERVED_COMMAND_WORDS

    def __post_init__(self) -> None:
        self._by_word: dict[str, CommandModule] = {}
        for module in self.modules:
            self._register(module)

    def _register(self, module: CommandModule) -> None:
        for word in module.words:
            upper = validate_word(word, what=f"{module.name} word")
            if module.operator and upper in self.reserved:
                raise ModuleError(f"{upper} is a built-in or reserved command word")
            if upper in self._by_word:
                raise ModuleError(f"{upper} is defined twice")
            self._by_word[upper] = module

    def add(self, module: CommandModule) -> None:
        self.modules.append(module)
        self._register(module)

    def lookup(self, word: str) -> CommandModule | None:
        return self._by_word.get(word.upper())

    def get(self, name: str | None) -> CommandModule | None:
        return self._by_word.get(name) if name else None

    def words(self) -> frozenset[str]:
        return frozenset(self._by_word)

    def services(self) -> list[Any]:
        return [m.service for m in self.modules if m.service is not None]

    def lab_words(self) -> frozenset[str]:
        return frozenset(w for w, m in self._by_word.items() if m.lab)

    def help(self, word: str, *, lab_unlocked: bool) -> str | None:
        module = self.lookup(word.strip())
        if module is None or (module.lab and not lab_unlocked):
            return None
        return module.usage

    def menu_more(self, *, lab_unlocked: bool, channel: bool = False) -> str | None:
        """`MENU MORE`: the optional commands this garden enabled, or None."""
        entries = [
            m.menu for m in self.modules
            if m.menu and not m.builtin and (lab_unlocked or not m.lab)
            and (m.channel_ok or not channel)
        ]
        if not entries:
            return None
        return "➕ More\n" + "\n".join(entries)

    def run(self, module: CommandModule, request: ModuleRequest) -> str:
        """Call the handler; a failing module answers alone and never takes
        the daemon or another command down with it."""
        try:
            body = module.handler(request)
        except Exception:
            log.exception("command module %s failed", module.name)
            return f"{module.name} unavailable right now."
        if not isinstance(body, str) or not body:
            log.error("command module %s returned no reply", module.name)
            return f"{module.name} unavailable right now."
        return body


def builtin_modules(
    *, latitude: float | None = None, longitude: float | None = None
) -> list[CommandModule]:
    """Core commands extracted onto the module interface (MOON, SUN). They
    keep their Command labels, channel availability and help text."""

    def moon(req: ModuleRequest) -> str:
        from meshgarden.moon import moon_text

        return moon_text(req.now)

    def sun(req: ModuleRequest) -> str:
        try:
            from meshgarden.sun import sun_text
        except ImportError:
            log.warning("SUN unavailable: synchronize dependencies using scripts/install-rpi.sh")
            return "☀️ SUN unavailable\nAstronomy data support needs updating."
        return sun_text(req.now, latitude=latitude, longitude=longitude,
                        timezone="America/Los_Angeles")

    return [
        CommandModule(
            name="MOON", command=Command.MOON, handler=moon, aliases=("!MOON",),
            usage="MOON (or !moon)\nApproximate phase and illumination; entirely offline",
            channel_ok=True, builtin=True,
        ),
        CommandModule(
            name="SUN", command=Command.SUN, handler=sun, aliases=("!SUN",),
            usage="SUN (or !sun)\nSunrise/sunset at the garden, today in local time; offline",
            channel_ok=True, builtin=True,
        ),
    ]


def build_registry(extra: Iterable[CommandModule] = (), **builtin_kwargs) -> ModuleRegistry:
    registry = ModuleRegistry(builtin_modules(**builtin_kwargs))
    for module in extra:
        registry.add(module)
    return registry


_DEFAULT: ModuleRegistry | None = None


def default_registry() -> ModuleRegistry:
    """Built-ins only: what `parse_command` uses when no registry is passed."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_registry()
    return _DEFAULT


# Bundled optional modules, one file each (meshgarden/mod_<name>.py) exposing
# `factory(cfg) -> list[CommandModule]`. A factory reads its own config
# section and returns [] when disabled, so a disabled module never
# initializes anything (no threads, no network clients). A fixed tuple, not a
# discovery mechanism: nothing outside this package can add a module.
BUNDLED_MODULES: tuple[str, ...] = ("mod_info", "mod_find", "mod_forecast", "mod_link")


def optional_factories() -> list[Callable[..., Iterable[CommandModule]]]:
    # Imported lazily: every mod_* file imports this module.
    return [importlib.import_module(f"meshgarden.{name}").factory for name in BUNDLED_MODULES]


def configured_registry(cfg) -> ModuleRegistry:
    lat, lon = cfg.observatory.latitude, cfg.observatory.longitude
    if (lat, lon) == (0.0, 0.0):
        lat = lon = None
    registry = build_registry(latitude=lat, longitude=lon)
    for factory in optional_factories():
        try:
            modules = list(factory(cfg))
        except ModuleError:
            raise
        except Exception:
            # One broken optional module must not keep the garden off the air.
            log.exception("command module factory %s failed; skipped", factory.__module__)
            continue
        for module in modules:
            registry.add(module)
    return registry
