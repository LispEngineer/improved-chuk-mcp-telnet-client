"""0.9.0 token-efficient results, through the real stdio MCP server and a local fake host.

No remote target is contacted. The client here is the plain one: it asks for nothing
extra, so it sees exactly what an agent sees by default.
"""

import base64

import pytest

from test_lossless_stream import Endpoint, connect, read, wait_bytes
from test_m1_live_reader import Mcp

RAW_FIELDS = ('raw_base64', 'initial_banner_raw_base64', 'final_output_raw_base64')


def has_raw(result):
    return [k for k in RAW_FIELDS if k in result]


@pytest.fixture
def peer(tmp_path):
    e = Endpoint()
    mcp = Mcp(str(tmp_path))
    yield mcp, e, tmp_path
    mcp.close()
    e.close()


# ---- (a) raw_base64 only with include_raw --------------------------------------------------


def test_a_default_results_carry_no_raw_fields(peer):
    mcp, e, _ = peer
    opening = mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=['C1'],
                                             telnet_session_id='t', response_wait=0.3,
                                             prompt_pattern=r'P> '))
    assert opening['initial_banner'] == 'READY> '
    assert opening['responses'][0]['response'] == 'ANSWER[C1]\r\nP> '
    assert has_raw(opening) == [] and 'raw_base64' not in opening['responses'][0]
    sent = mcp.tool('telnet_send_input', dict(session_id='t', input_text='C2', prompt_pattern=r'P> '))
    assert sent['response'].startswith('TAIL\r\nANSWER[C2]') and has_raw(sent) == []
    got = mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0))
    assert got['output'] == 'TAIL\r\n' and has_raw(got) == []
    assert got['new_bytes_count'] == 6
    closed = mcp.tool('telnet_close_session', dict(session_id='t'))
    assert closed['success'] is True and has_raw(closed) == []


def test_a_include_raw_returns_the_exact_bytes_everywhere(peer):
    mcp, e, _ = peer
    opening = mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=['C1'],
                                             telnet_session_id='t', response_wait=0.3,
                                             prompt_pattern=r'P> ', include_raw=True))
    assert base64.b64decode(opening['initial_banner_raw_base64']) == b'READY> '
    assert base64.b64decode(opening['responses'][0]['raw_base64']) == b'ANSWER[C1]\r\nP> '
    assert opening['final_output_raw_base64'] == ''
    e.send(b'MORE\r\n')
    wait_bytes(mcp, e, len(e.sent), 't')
    got = mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0, include_raw=True))
    assert base64.b64decode(got['raw_base64']) == b'TAIL\r\nMORE\r\n'
    closed = mcp.tool('telnet_close_session', dict(session_id='t', include_raw=True))
    assert closed['raw_base64'] == ''


@pytest.mark.parametrize('transport', ['telnet', 'serial'])
def test_a_include_raw_round_trip_is_byte_exact(transport, tmp_path):
    """Every byte value the host sends comes back identically from the base64 field."""
    e = Endpoint(transport)
    mcp = Mcp(str(tmp_path))
    try:
        connect(mcp, e)
        blob = bytes(b for b in range(256) if b not in (0xff, 0x0a, 0x0d)) + b'\xe2\x82\xac' + b'\r\nEND> '
        e.send(blob)
        wait_bytes(mcp, e, len(e.sent))
        r = mcp.tool(transport + '_read_session', dict(session_id='lossless', max_wait_seconds=0,
                                                       include_raw=True))
        assert base64.b64decode(r['raw_base64']) == blob
        assert r['new_bytes_count'] == len(blob)
    finally:
        mcp.close()
        e.close()


def test_a_serial_tools_accept_include_raw(tmp_path):
    e = Endpoint('serial')
    mcp = Mcp(str(tmp_path))
    try:
        opening = mcp.tool('serial_client', dict(port=e.port, commands=[], serial_session_id='s',
                                                 response_wait=0.3, include_raw=True))
        assert 'initial_banner_raw_base64' in opening
        plain = mcp.tool('serial_send_input', dict(session_id='s', input_text='C', prompt_pattern=r'P> '))
        assert has_raw(plain) == []
        e.send(b'X> ')
        wait_bytes(mcp, e, len(e.sent), 's')
        closed = mcp.tool('serial_close_session', dict(session_id='s', include_raw=True))
        assert base64.b64decode(closed['raw_base64'])
    finally:
        mcp.close()
        e.close()


def test_a_missing_session_close_with_include_raw_still_answers(peer):
    mcp, e, _ = peer
    r = mcp.tool('telnet_close_session', dict(session_id='nope', include_raw=True))
    assert r['success'] is False and r['raw_base64'] == ''
    assert has_raw(mcp.tool('telnet_close_session', dict(session_id='nope'))) == []


# ---- (b) annotated_text only when it differs from screen_text ---------------------------------


def test_b_annotated_text_omitted_when_same_present_when_different(peer):
    mcp, e, _ = peer
    mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[],
                                   telnet_session_id='t', response_wait=0.3))
    e.send(b'\x1b[2J\x1b[Hplain line\r\nsecond')
    wait_bytes(mcp, e, len(e.sent), 't')
    screen = mcp.tool('terminal_get_screen', dict(session_id='t'))
    assert 'plain line' in screen['screen_text'] and 'annotated_text' not in screen
    key = mcp.tool('terminal_send_key', dict(session_id='t', key='SPACE', wait_seconds=0.1))
    assert 'annotated_text' not in key
    waited = mcp.tool('terminal_wait_for_change', dict(session_id='t', timeout_seconds=0.3,
                                                       settle_seconds=0.05))
    assert 'annotated_text' not in waited

    e.send(b'\x1b[2J\x1b[H\x1b[7mSELECTED\x1b[0m normal')
    wait_bytes(mcp, e, len(e.sent), 't')
    screen = mcp.tool('terminal_get_screen', dict(session_id='t'))
    assert screen['annotated_text'].startswith('* [SELECTED] *')
    assert screen['annotated_text'] != screen['screen_text']
    assert screen['highlighted_lines'] == [1]
    key = mcp.tool('terminal_send_key', dict(session_id='t', key='SPACE', wait_seconds=0.1))
    assert key['annotated_text'].startswith('* [SELECTED] *')


def test_b_wait_for_change_still_sees_a_reverse_video_only_change(peer):
    """The change baseline compares the full annotated text, not the trimmed result."""
    mcp, e, _ = peer
    mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[],
                                   telnet_session_id='t', response_wait=0.3))
    e.send(b'\x1b[2J\x1b[HMENU')
    wait_bytes(mcp, e, len(e.sent), 't')
    mcp.tool('terminal_get_screen', dict(session_id='t'))
    e.send(b'\x1b[H\x1b[7mMENU\x1b[0m')
    wait_bytes(mcp, e, len(e.sent), 't')
    waited = mcp.tool('terminal_wait_for_change', dict(session_id='t', timeout_seconds=2,
                                                       settle_seconds=0.1))
    assert waited['changed'] is True
    assert waited['annotated_text'].startswith('* [MENU] *')


# ---- (c) raw_base64 is included automatically when the text cannot carry the bytes ----------------


def test_c_invalid_utf8_includes_raw_and_says_why(peer):
    mcp, e, tmp = peer
    mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[], telnet_session_id='t',
                                   response_wait=0.3))
    e.send(b'BAD\x80\x81\xc3(\x1b[?25h\r\nEND> ')
    wait_bytes(mcp, e, len(e.sent), 't')
    r = mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0))
    assert base64.b64decode(r['raw_base64']) == b'BAD\x80\x81\xc3(\x1b[?25h\r\nEND> '
    assert r['raw_included_reason'] == 'invalid_or_split_utf8'
    assert '�' in r['output']
    # an exact delivery afterwards is plain again
    e.send(b'clean \xe2\x82\xac\r\n')
    wait_bytes(mcp, e, len(e.sent), 't')
    r = mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0))
    assert r['output'] == 'clean €\r\n' and has_raw(r) == [] and 'raw_included_reason' not in r


def test_c_include_raw_true_has_no_reason_even_when_inexact(peer):
    mcp, e, _ = peer
    mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[], telnet_session_id='t',
                                   response_wait=0.3))
    e.send(b'\xff\x80')
    wait_bytes(mcp, e, len(e.sent), 't')
    r = mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0, include_raw=True))
    assert base64.b64decode(r['raw_base64']) == b'\xff\x80' and 'raw_included_reason' not in r


def test_c_character_split_across_deliveries_is_flagged_on_both_sides(peer):
    mcp, e, _ = peer
    mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[], telnet_session_id='t',
                                   response_wait=0.3))
    e.send(b'euro \xe2\x82')
    wait_bytes(mcp, e, len(e.sent), 't')
    first = mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0))
    assert first['output'] == 'euro ' and first['raw_included_reason'] == 'invalid_or_split_utf8'
    e.send(b'\xac done')
    wait_bytes(mcp, e, len(e.sent), 't')
    second = mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0))
    assert second['output'] == '€ done' and second['raw_included_reason'] == 'invalid_or_split_utf8'
    joined = base64.b64decode(first['raw_base64']) + base64.b64decode(second['raw_base64'])
    assert joined == b'euro \xe2\x82\xac done'


def test_c_command_responses_and_banner_carry_their_own_raw(tmp_path):
    e = Endpoint()
    mcp = Mcp(str(tmp_path))
    try:
        opening = mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=['C1'],
                                                 telnet_session_id='t', response_wait=0.3,
                                                 prompt_pattern=r'P> '))
        assert has_raw(opening) == [] and 'raw_included_reason' not in opening
        e.send(b'\x80 more\r\nQ> ')
        wait_bytes(mcp, e, len(e.sent), 't')
        r = mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=['C2'],
                                           telnet_session_id='t', prompt_pattern=r'P> ',
                                           close_session=True))
        first = r['responses'][0]
        assert first['raw_included_reason'] == 'invalid_or_split_utf8'
        assert base64.b64decode(first['raw_base64']).startswith(b'TAIL\r\n\x80 more')
    finally:
        mcp.close()
        e.close()


def test_c_close_session_flags_a_split_character(peer):
    mcp, e, _ = peer
    mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[], telnet_session_id='t',
                                   response_wait=0.3))
    e.send(b'tail\xe2')
    wait_bytes(mcp, e, len(e.sent), 't')
    closed = mcp.tool('telnet_close_session', dict(session_id='t'))
    assert base64.b64decode(closed['raw_base64']) == b'tail\xe2'
    assert closed['raw_included_reason'] == 'invalid_or_split_utf8'


def test_c_exact_bytes_stay_in_the_raw_transcript_without_include_raw(peer):
    mcp, e, tmp = peer
    mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[], telnet_session_id='t',
                                   response_wait=0.3))
    e.send(b'\x80\xfe\xc3(\x1b[1m ok \xe2\x82\xac\r\n')
    wait_bytes(mcp, e, len(e.sent), 't')
    mcp.tool('telnet_read_session', dict(session_id='t', max_wait_seconds=0))
    mcp.tool('telnet_close_session', dict(session_id='t'))
    transcripts = list(tmp.glob('*.log.raw'))
    assert len(transcripts) == 1
    assert transcripts[0].read_bytes() == bytes(e.sent)
