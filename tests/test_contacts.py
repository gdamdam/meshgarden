from meshgarden.contacts import RadioContact, select_contacts_to_prune

NOW = 1_800_000_000


def _c(n, *, adv_type=1, last_advert=None):
    return RadioContact(f"{n:08x}" + "00" * 28, f"!{n:08x}", None, adv_type, last_advert)


def _select(contacts, *, target, max_remove=100, seen=None, protected=()):
    return select_contacts_to_prune(
        contacts, target=target, max_remove=max_remove, now=NOW,
        last_seen_by_node=seen or {}, protected_nodes=protected,
    )


def test_under_or_at_target_removes_nothing():
    cs = [_c(1), _c(2)]
    assert _select(cs, target=2).remove == []
    assert _select(cs, target=5).remove == []


def test_repeaters_rooms_and_protected_are_never_removed():
    cs = [_c(1, adv_type=2), _c(2, adv_type=3), _c(3), _c(4)]
    d = _select(cs, target=0, protected={"!00000003"})
    assert [c.node_id for c in d.remove] == ["!00000004"]
    assert d.kept_protected == 3 and d.over_target == 4


def test_least_heard_first_using_max_of_db_and_advert():
    cs = [_c(1, last_advert=500), _c(2), _c(3, last_advert=100)]
    seen = {"!00000002": 300, "!00000003": 900}
    d = _select(cs, target=1, seen=seen)
    # heard: 1 -> 500, 2 -> 300, 3 -> max(900, 100) = 900
    assert [c.node_id for c in d.remove] == ["!00000002", "!00000001"]
    assert d.kept_recent == 1


def test_capped_by_target_gap_and_by_max_remove():
    cs = [_c(i) for i in range(1, 11)]
    assert len(_select(cs, target=8).remove) == 2
    d = _select(cs, target=2, max_remove=3)
    assert len(d.remove) == 3 and d.over_target == 8


def test_missing_advert_counts_as_zero_and_ties_break_by_node_id():
    cs = [_c(3), _c(1), _c(2, last_advert=50)]
    d = _select(cs, target=0)
    assert [c.node_id for c in d.remove] == ["!00000001", "!00000003", "!00000002"]
