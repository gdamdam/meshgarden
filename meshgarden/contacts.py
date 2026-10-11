"""Pure selection logic for the daily MeshCore contact-table housekeeping.

A radio whose contact table is full refuses to auto-add new nodes, so the
garden cannot decrypt a newcomer's DMs or attribute their channel messages.
This module only DECIDES what to evict; `main.py` does the radio I/O. It
imports nothing from the radio stack so it stays trivially testable.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

# MeshCore advert types: 1 companion/chat, 2 repeater, 3 room server.
ADV_TYPE_COMPANION = 1


@dataclass(frozen=True)
class RadioContact:
    public_key: str  # full hex pubkey as the library gives it
    node_id: str  # "!" + first 8 hex chars, lowercased
    adv_name: str | None
    adv_type: int  # unknown values are normalized to 1 by the client
    last_advert: int | None  # epoch seconds; None when absent or implausible


@dataclass(frozen=True)
class PruneDecision:
    remove: list[RadioContact]  # least-recently-heard first
    kept_protected: int
    kept_recent: int
    over_target: int  # len(contacts) - target, before capping


def select_contacts_to_prune(
    contacts: Sequence[RadioContact],
    *,
    target: int,
    max_remove: int,
    now: int,
    last_seen_by_node: Mapping[str, int],
    protected_nodes: Collection[str],
) -> PruneDecision:
    """`now` is unused by the ordering today; it stays in the signature so a
    future grace window ("never evict something heard in the last N hours")
    does not change every caller."""
    over = len(contacts) - target
    eligible: list[tuple[int, str, RadioContact]] = []
    protected = 0
    for contact in contacts:
        if contact.adv_type != ADV_TYPE_COMPANION or contact.node_id in protected_nodes:
            protected += 1
            continue
        heard = max(last_seen_by_node.get(contact.node_id, 0), contact.last_advert or 0)
        eligible.append((heard, contact.node_id, contact))
    if over <= 0:
        return PruneDecision(remove=[], kept_protected=protected, kept_recent=len(eligible),
                             over_target=over)
    eligible.sort(key=lambda item: (item[0], item[1]))
    count = max(0, min(over, max_remove, len(eligible)))
    return PruneDecision(
        remove=[item[2] for item in eligible[:count]],
        kept_protected=protected,
        kept_recent=len(eligible) - count,
        over_target=over,
    )
