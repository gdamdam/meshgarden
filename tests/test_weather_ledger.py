"""WeatherLedger state machine against a real SQLite file (spec section 5)."""
from __future__ import annotations

from meshgarden.database import connect, migrate
from meshgarden.weather import DEFAULT_ZONE_ALIASES, WeatherLedger, WeatherProduct

TZ = "America/Los_Angeles"
NOW = 1791830000
EXP = NOW + 6 * 3600


def product(key="a1", *, ids=None, refs=(), event="Red Flag Warning", mtype="Alert",
            zones=("CAZ504",), expires=EXP):
    return WeatherProduct(
        product_key=key, alert_ids=tuple(ids or (key,)), references=tuple(refs),
        event=event, message_type=mtype, severity="Severe", zones=zones,
        sent_at=NOW - 60, expires_at=expires,
    )


def ledger(tmp_path, cap=6):
    conn = connect(str(tmp_path / "o.db"))
    migrate(conn)
    return WeatherLedger(conn, max_sends_per_hour=cap, aliases=DEFAULT_ZONE_ALIASES, timezone=TZ)


def rows(led):
    return {r["product_key"]: dict(r) for r in led._conn.execute("SELECT * FROM weather_alerts")}


def test_first_sighting_plans_an_alert_and_records_sent(tmp_path):
    led = ledger(tmp_path)
    plans = led.apply([product()], NOW)
    assert [(p.product_key, p.kind) for p in plans] == [("a1", "Alert")]
    assert plans[0].line.startswith("⚠ Red Flag Warning · North Bay Mtns · until ")
    assert rows(led)["a1"]["outcome"] == "sent"


def test_identical_poll_plans_nothing(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    assert led.apply([product()], NOW + 120) == []


def test_update_with_same_fields_is_suppressed(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    upd = product("a2", refs=("a1",), mtype="Update")
    assert led.apply([upd], NOW + 120) == []
    r = rows(led)["a1"]
    assert r["last_alert_id"] == "a2" and r["outcome"] == "sent"


def test_update_with_new_expiry_plans_an_update_line(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    upd = product("a2", refs=("a1",), mtype="Update", expires=EXP + 3600)
    plans = led.apply([upd], NOW + 120)
    assert [(p.product_key, p.kind) for p in plans] == [("a1", "Update")]
    assert plans[0].line.startswith("↻ ")
    assert rows(led)["a1"]["expires_at"] == EXP + 3600


def test_update_for_unseen_product_is_announced_as_alert(tmp_path):
    led = ledger(tmp_path)
    plans = led.apply([product("b2", refs=("b1",), mtype="Update")], NOW)
    assert [(p.product_key, p.kind) for p in plans] == [("b2", "Alert")]


def test_cancel_sends_once_and_unseen_cancel_is_ignored(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    can = product("a3", refs=("a1",), mtype="Cancel")
    plans = led.apply([can], NOW + 60)
    assert [(p.product_key, p.kind) for p in plans] == [("a1", "Cancel")]
    assert plans[0].line.startswith("✓ Red Flag Warning cancelled")
    assert led.apply([can], NOW + 120) == []
    assert led.apply([product("zz", refs=("never",), mtype="Cancel")], NOW) == []


def test_expiry_is_silent(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    assert led.apply([], EXP + 10) == []


def test_hourly_cap_records_capped_then_retries(tmp_path):
    led = ledger(tmp_path, cap=1)
    led.apply([product("a1")], NOW)
    assert led.apply([product("b1", zones=("CAZ006",))], NOW + 10) == []
    assert rows(led)["b1"]["outcome"] == "capped"
    plans = led.apply([product("b1", zones=("CAZ006",))], NOW + 3601)
    assert [(p.product_key, p.kind) for p in plans] == [("b1", "Alert")]


def test_max_plans_per_batch_leaves_the_rest_for_the_next_poll(tmp_path):
    led = ledger(tmp_path)
    prods = [product(f"p{i}", zones=("CAZ006",)) for i in range(3)]
    assert len(led.apply(prods, NOW)) == 2
    assert len(led.apply(prods, NOW + 120)) == 1


def test_mark_dropped_then_retry_once(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    led.mark("a1", "dropped", NOW + 1)
    plans = led.apply([product()], NOW + 120)
    assert [(p.product_key, p.kind) for p in plans] == [("a1", "Alert")]
    assert rows(led)["a1"]["outcome"] == "sent"


def test_restart_with_populated_table_sends_nothing(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    led2 = WeatherLedger(led._conn, max_sends_per_hour=6, aliases=DEFAULT_ZONE_ALIASES, timezone=TZ)
    assert led2.apply([product()], NOW + 5) == []


def test_prune_removes_rows_seven_days_after_expiry(tmp_path):
    led = ledger(tmp_path)
    led.apply([product()], NOW)
    assert led.prune(EXP + 7 * 86400 - 1) == 0
    assert led.prune(EXP + 7 * 86400 + 1) == 1


def test_capped_cancel_is_retried_once(tmp_path):
    led = ledger(tmp_path, cap=1)
    led.apply([product("a1")], NOW)
    assert led.apply([product("c1", refs=("a1",), mtype="Cancel")], NOW + 10) == []
    assert rows(led)["a1"]["outcome"] == "capped"
    plans = led.apply([product("c1", refs=("a1",), mtype="Cancel")], NOW + 3601)
    assert [(p.product_key, p.kind) for p in plans] == [("a1", "Cancel")]
    assert led.apply([product("c1", refs=("a1",), mtype="Cancel")], NOW + 3700) == []


def test_hourly_cap_counts_every_send_not_every_product(tmp_path):
    led = ledger(tmp_path, cap=2)
    led.apply([product("a1")], NOW)
    plans = led.apply([product("a2", refs=("a1",), mtype="Update", expires=EXP + 3600)], NOW + 5)
    assert [p.kind for p in plans] == ["Update"]
    assert led.apply([product("b1", zones=("CAZ006",))], NOW + 10) == []
    assert rows(led)["b1"]["outcome"] == "capped"


def test_active_count_excludes_cancel_expired_and_dropped(tmp_path):
    led = ledger(tmp_path)
    led.apply(
        [
            product("a1", zones=("CAZ509",)),
            product("a2", event="Wind Advisory Warning", zones=("CAZ006",)),
        ],
        NOW,
    )
    led.apply([product("a3", event="Flood Watch", zones=("CAZ508",))], NOW + 1)
    led._conn.execute(
        "UPDATE weather_alerts SET expires_at = ? WHERE product_key = 'a3'", (NOW + 10,)
    )
    led.mark("a2", "dropped", NOW)
    assert led.active_count(NOW) == 2
    assert led.active_count(NOW + 20) == 1  # a3 expired
    led._conn.execute("UPDATE weather_alerts SET message_type = 'Cancel' WHERE product_key = 'a1'")
    assert led.active_count(NOW + 20) == 0


def test_last_sent_at_none_then_set(tmp_path):
    led = ledger(tmp_path)
    assert led.last_sent_at() is None
    led.apply([product("a1")], NOW)
    assert led.last_sent_at() == NOW
    led.mark("a1", "dropped", NOW + 5)
    assert led.last_sent_at() is None


def test_state_round_trip_and_overwrite(tmp_path):
    led = ledger(tmp_path)
    assert led.get_state("heartbeat_day") is None
    led.set_state("heartbeat_day", "2026-10-10", NOW)
    assert led.get_state("heartbeat_day") == "2026-10-10"
    led.set_state("heartbeat_day", "2026-10-11", NOW + 1)
    assert led.get_state("heartbeat_day") == "2026-10-11"
