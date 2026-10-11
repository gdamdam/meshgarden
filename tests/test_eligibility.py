import pytest

from meshgarden.commands import Reject, check_eligibility
from meshgarden.radio import extract
from tests.fakes import make_packet

ME = 0x11223344


def ob(**kw):
    return extract(make_packet(**kw), now=1000, now_monotonic=1.0)


def elig(**kw):
    return check_eligibility(ob(**kw), my_node_num=ME, reply_ready=True)


@pytest.mark.parametrize("channel", [0, 1, 2, 3, 4, 5, 6, 7])
def test_broadcast_is_rejected_on_every_channel(channel):
    r = elig(to_num=0xFFFFFFFF, channel=channel, text="HELP")
    assert r.eligible is False and r.reason is Reject.BROADCAST


@pytest.mark.parametrize("channel", [0, 1, 2, 3, 4, 5, 6, 7])
def test_broadcast_sign_is_rejected_on_every_channel(channel):
    r = elig(to_num=0xFFFFFFFF, channel=channel, text="SIGN hello world")
    assert r.eligible is False and r.reason is Reject.BROADCAST


def test_dm_on_channel_zero_is_eligible():
    assert elig(to_num=ME, channel=0, text="HELP").eligible is True


@pytest.mark.parametrize("channel", [0, 1, 2, 3, 4, 5, 6, 7])
def test_dm_is_eligible_regardless_of_channel_index(channel):
    assert elig(to_num=ME, channel=channel, text="HELP").eligible is True


def test_dm_to_another_node_is_rejected():
    r = elig(to_num=0x99999999, text="HELP")
    assert r.reason is Reject.NOT_FOR_US


def test_self_echo_is_rejected():
    r = elig(from_num=ME, to_num=ME, text="HELP")
    assert r.reason is Reject.SELF_ORIGIN


def test_non_text_packet_is_rejected():
    r = elig(to_num=ME, portnum="POSITION_APP", text=None)
    assert r.reason is Reject.NOT_TEXT


def test_undecodable_text_is_rejected():
    r = elig(to_num=ME, portnum="TEXT_MESSAGE_APP", text=None)
    assert r.reason is Reject.NO_TEXT


def test_missing_packet_id_is_rejected():
    r = elig(to_num=ME, packet_id=None, text="HELP")
    assert r.reason is Reject.NO_PACKET_ID


def test_not_reply_ready_blocks_eligibility():
    r = check_eligibility(ob(to_num=ME, text="HELP"), my_node_num=ME, reply_ready=False)
    assert r.reason is Reject.NOT_REPLY_READY


def test_pki_dm_is_eligible():
    assert elig(to_num=ME, pki_encrypted=True, text="HELP").eligible is True
