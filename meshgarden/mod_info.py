"""Operator-defined information commands (WP3): `[[commands.info]]`.

Each enabled entry answers with its configured text, verbatim. There is no
templating, file access or shell: the response is data, not code.
"""

from __future__ import annotations

from meshgarden.commands import Command
from meshgarden.modules import CommandModule, ModuleRequest

_USAGE_MAX_BYTES = 180


def _usage(name: str, help_text: str) -> str:
    text = f"{name}\n{help_text or 'Information from this garden'}"
    # help is validated <= 180 bytes; the name line can push it over.
    return text.encode("utf-8")[:_USAGE_MAX_BYTES].decode("utf-8", "ignore")


def factory(cfg) -> list[CommandModule]:
    modules = []
    for entry in cfg.commands.info:
        if not entry.enabled:
            continue

        def handler(_req: ModuleRequest, _text: str = entry.response) -> str:
            return _text

        modules.append(CommandModule(
            name=entry.name,
            command=Command.CUSTOM,
            usage=_usage(entry.name, entry.help),
            handler=handler,
            aliases=entry.aliases,
            menu=f"{entry.name} {entry.help}".strip() if entry.help else entry.name,
            channel_ok=entry.scope == "channel",
            operator=True,
        ))
    return modules
