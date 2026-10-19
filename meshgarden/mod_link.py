"""LINK / UNLINK: join one person's MeshCore and Meshtastic radios (WP10).

DM-only: these change account state, and a channel sender's name is not
authenticated. The policies live in meshgarden/accounts.py.
"""

from __future__ import annotations

from meshgarden import accounts, bbs_ids
from meshgarden.commands import Command
from meshgarden.fortune import local_day
from meshgarden.modules import CommandModule, ModuleRequest

_MESH = {"meshcore": "MeshCore", "meshtastic": "Meshtastic"}

_FAILURES = {
    "bad_code": "🔗 LINK failed: code unknown or expired.",
    "same_mesh": "🔗 LINK failed: send the code from your radio on the other mesh.",
    "already_linked": "🔗 Already linked. UNLINK first.",
    "blocked": "🔗 LINK unavailable.",
    "too_many_attempts": "🔗 Too many LINK attempts today. Try tomorrow.",
}


def _other_mesh(identity: str) -> str:
    own = bbs_ids.split(identity)[0]
    return _MESH["meshtastic" if own == "meshcore" else "meshcore"]


def factory(cfg) -> list[CommandModule]:
    if not cfg.service.identity_linking:
        return []
    salt = cfg.service.fortune_salt
    timezone = cfg.service.timezone

    def link(req: ModuleRequest) -> str:
        assert req.conn is not None
        if req.channel:
            return "🔗 LINK works by DM only."
        try:
            if not req.argument:
                code = accounts.start(req.conn, req.identity, req.now)
                return (
                    f"🔗 Link code {code[:4]}-{code[4:]}\n"
                    f"Within 10 min, DM LINK {code} from your {_other_mesh(req.identity)} "
                    "radio. Never share this code."
                )
            accounts.complete(req.conn, req.identity, req.argument, req.now, salt=salt)
        except accounts.LinkError as exc:
            return _FAILURES.get(exc.reason, _FAILURES["bad_code"])
        return (
            f"🔗 Linked with your {_other_mesh(req.identity)} radio.\n"
            "Mail, LAB and games are now shared. UNLINK to undo."
        )

    def unlink(req: ModuleRequest) -> str:
        assert req.conn is not None
        if req.channel:
            return "🔗 UNLINK works by DM only."
        leaving = accounts.unlink(
            req.conn, req.identity, req.now, salt=salt, today=local_day(req.now, timezone)
        )
        if leaving is None:
            return "🔗 This radio is not linked."
        if leaving == req.identity:
            return "🔗 Unlinked. This radio starts with its own empty mailbox."
        return "🔗 Unlinked. This radio keeps the shared mailbox."

    return [
        CommandModule(
            name="LINK", command=Command.LINK, handler=link, takes_argument=True,
            usage=("LINK | LINK <code>\nJoin your MeshCore and Meshtastic radios: LINK on one, "
                   "then LINK <code> on the other within 10 min. DM only"),
            menu="LINK join your two radios",
        ),
        CommandModule(
            name="UNLINK", command=Command.LINK, handler=unlink,
            usage="UNLINK\nSplit linked radios; the radio that started the link keeps the mail",
        ),
    ]
