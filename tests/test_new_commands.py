import json
from datetime import UTC, datetime

import pytest

from meshgarden.commands import Command, parse_command
from meshgarden.database import connect, migrate
from meshgarden.diagnostics import route_reply
from meshgarden.models import MeshCoreRoute
from meshgarden.moon import moon_text
from meshgarden.radio import extract
from tests.fakes import make_packet


@pytest.mark.parametrize('text,command,arg', [
    ('!moon', Command.MOON, ''),
    ('PATH', Command.PATH, ''), ('!route', Command.PATH, ''),
])
def test_new_commands_and_exact_aliases(text, command, arg):
    parsed = parse_command(text)
    assert (parsed.command, parsed.argument) == (command, arg)


@pytest.mark.parametrize('text', ['!sign hi', '!iss extra', '!meteor', 'METEOR 2-2',
                                  '!path somebody'])
def test_new_aliases_do_not_expand_arbitrary_commands(text):
    assert parse_command(text).command == Command.UNKNOWN


def test_guestbook_with_a_non_ascii_digit_argument_falls_back_to_unknown():
    """Regression: '²'.isdigit() is True but int('²') raises
    ValueError -- _parse_guestbook_argument must guard with isascii()
    first instead of crashing. '²' matches neither the #<id> nor the
    bare-page-number grammar, so it's the same silent UNKNOWN as any other
    unrecognized argument."""
    parsed = parse_command('GUESTBOOK ²')
    assert parsed.command == Command.UNKNOWN


def test_offline_moon_has_distinct_new_full_and_waning_phases():
    epoch = int(datetime(2000, 1, 6, 18, 14, tzinfo=UTC).timestamp())
    cycle = 29.53058867 * 86400
    assert 'New moon' in moon_text(epoch)
    assert '~0%' in moon_text(epoch)
    assert 'Full moon' in moon_text(int(epoch + cycle / 2))
    assert '~100%' in moon_text(int(epoch + cycle / 2))
    assert 'Last quarter' in moon_text(int(epoch + cycle * .75))
    assert '~50%' in moon_text(int(epoch + cycle * .75))
    assert moon_text(epoch).splitlines() == [
        '🌑 MOON · New moon',
        '💡 ~0% illuminated · approx',
    ]


def test_route_shows_uppercase_prefixes_and_preserves_stored_hashes(tmp_path):
    from meshgarden.collector import Collector

    conn = connect(str(tmp_path / 'routes.db'))
    migrate(conn)
    Collector(conn).record_meshcore_route(
        MeshCoreRoute('!aabbccdd', 63, ('ab', 'Cd12', 'ef3456') * 21, 1000)
    )
    obs = extract(make_packet(text='PATH', rx_snr=4.5), now=1600, now_monotonic=0)
    body = route_reply(conn, '!aabbccdd', obs, meshcore=True, now=1600, max_bytes=120)
    assert 'RX: hops unknown' in body
    assert 'TX cache 10m' in body
    assert 'unverified' in body
    assert 'AB>CD>EF' in body
    assert 'Cd12' not in body and 'ef3456' not in body
    stored = conn.execute('SELECT stored_route FROM nodes WHERE node_id = ?',
                          ('!aabbccdd',)).fetchone()[0]
    assert json.loads(stored)['hashes'] == ['ab', 'Cd12', 'ef3456'] * 21
    assert '(+' in body
    assert len(body.encode()) <= 120
    conn.close()


def test_route_never_turns_missing_or_empty_cache_into_direct_rx(tmp_path):
    conn = connect(str(tmp_path / 'routes.db'))
    migrate(conn)
    obs = extract(make_packet(text='PATH'), now=1600, now_monotonic=0)
    for backend in (True, False):
        body = route_reply(conn, '!aabbccdd', obs, meshcore=backend, now=1600, max_bytes=120)
        assert 'hops unknown' in body
        assert 'unavailable' in body
    conn.execute('INSERT INTO nodes (node_id,observed_first_seen,observed_last_seen,stored_route) '
                 'VALUES (?,0,0,?)', ('!aabbccdd', json.dumps(
                     {'hops': 0, 'hashes': [], 'cached_at': 1000})))
    body = route_reply(conn, '!aabbccdd', obs, meshcore=True, now=1600, max_bytes=120)
    assert 'hops unknown' in body
    assert 'not RX hop evidence' in body
    conn.close()


@pytest.mark.parametrize('text,needle', [
    ('!moon', 'MOON'), ('!sun', 'SUN'), ('!path', 'PATH RX'),
])
def test_new_commands_reply_only_to_requester(daemon, text, needle):
    daemon.iface.emit_packet(make_packet(text=text))
    daemon.drain()
    assert len(daemon.iface.sent) == 1
    assert daemon.iface.sent[0]['destinationId'] == '!aabbccdd'
    assert needle in daemon.iface.sent[0]['text']
    assert len(daemon.iface.sent[0]['text'].encode()) <= 180


@pytest.mark.parametrize('text', ['!moon', '!sun', '!path'])
def test_new_commands_stay_silent_on_channels(daemon, text):
    daemon.iface.emit_packet(make_packet(text=text, to_num=0xffffffff))
    daemon.drain()
    assert daemon.iface.sent == []
