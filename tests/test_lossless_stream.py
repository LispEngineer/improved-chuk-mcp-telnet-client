"""Lossless delivery qualification using the checkout's actual stdio MCP runner.

No remote target is contacted. Long stress tests are opt-in with LOSSLESS_LONG=1.
Each stress transport sends all 10,000 numbered lines at seeded 0–50 ms intervals.
"""

import base64
import hashlib
import json
import os
import pty
import random
import select
import socket
import threading
import time
import tty
from pathlib import Path

import pytest

from test_m1_live_reader import Mcp


def payload(result, field='raw_base64'):
    return base64.b64decode(result[field], validate=True)


class Endpoint:
    """One TCP or PTY peer, with all writes and expected-byte accounting serialized."""

    def __init__(self, transport='telnet'):
        self.transport = transport
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.sent = bytearray()
        self.commands = []
        if transport == 'telnet':
            self.listener = socket.socket()
            self.listener.bind(('127.0.0.1', 0))
            self.listener.listen(1)
            self.port = self.listener.getsockname()[1]
        else:
            self.master, self.slave = pty.openpty()
            tty.setraw(self.master)
            tty.setraw(self.slave)
            self.port = os.ttyname(self.slave)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        if self.transport == 'telnet':
            self.conn, _ = self.listener.accept()
            self.conn.setblocking(True)
        self.ready.set()
        # Serial drivers may flush input at open; don't emit a pre-open banner.
        if self.transport == 'telnet':
            self.send(b'READY> ')
        buf = b''
        while not self.stop.is_set():
            try:
                if self.transport == 'telnet':
                    if not select.select([self.conn], [], [], 0.1)[0]:
                        continue
                    data = self.conn.recv(4096)
                else:
                    if not select.select([self.master], [], [], 0.1)[0]:
                        continue
                    data = os.read(self.master, 4096)
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    line, _, buf = buf.partition(b'\n')
                    self.commands.append(line.rstrip(b'\r'))
                    self.send(b'ANSWER[' + line.rstrip(b'\r') + b']\r\nP> TAIL\r\n')
            except socket.timeout:
                continue
            except OSError:
                break

    def send(self, data):
        self.ready.wait(10)
        with self.lock:
            if self.transport == 'telnet':
                self.conn.sendall(data.replace(b'\xff', b'\xff\xff'))
            else:
                view = memoryview(data)
                while view:
                    n = os.write(self.master, view)
                    view = view[n:]
            self.sent.extend(data)

    def eof(self):
        self.stop.set()
        if self.transport == 'telnet':
            self.conn.shutdown(socket.SHUT_WR)

    def close(self):
        self.stop.set()
        if self.transport == 'telnet':
            self.listener.close()
            if hasattr(self, 'conn'):
                self.conn.close()
        else:
            os.close(self.master)
            os.close(self.slave)
        self.thread.join(1)


def connect(mcp, endpoint, sid='lossless'):
    if endpoint.transport == 'telnet':
        return mcp.tool('telnet_client', dict(host='127.0.0.1', port=endpoint.port,
                       commands=[], telnet_session_id=sid, response_wait=0.3))
    return mcp.tool('serial_client', dict(port=endpoint.port, commands=[],
                   serial_session_id=sid, response_wait=0.3))


def read(mcp, endpoint, sid='lossless', **kwargs):
    return mcp.tool(endpoint.transport + '_read_session', dict(session_id=sid,
                    max_wait_seconds=0, **kwargs))


def wait_bytes(mcp, endpoint, count, sid='lossless', timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sessions = mcp.tool('list_sessions', {})
        if sessions['sessions'][sid]['total_bytes_received'] >= count:
            return
        time.sleep(0.01)
    raise AssertionError(f'reader did not ingest {count} bytes')


@pytest.fixture
def peer(tmp_path):
    endpoint = Endpoint()
    mcp = Mcp(str(tmp_path))
    initial = connect(mcp, endpoint)
    assert payload(initial, 'initial_banner_raw_base64') == b'READY> '
    yield mcp, endpoint, tmp_path
    mcp.close()
    endpoint.close()


def test_a2_preexisting_output_first_in_send(peer):
    mcp, e, _ = peer
    e.send(b'OLDER\r\nP> OLDTAIL\r\n')
    wait_bytes(mcp, e, len(e.sent))
    r = mcp.tool('telnet_send_input', dict(session_id='lossless', input_text='C',
                 prompt_pattern=r'P> ', max_wait_seconds=2))
    assert payload(r) == b'OLDER\r\nP> OLDTAIL\r\nANSWER[C]\r\nP> '
    assert payload(read(mcp, e)) == b'TAIL\r\n'
    assert payload(read(mcp, e)) == b''


@pytest.mark.parametrize('transport', ['telnet', 'serial'])
def test_a3_prompt_tail(transport, tmp_path):
    e = Endpoint(transport)
    mcp = Mcp(str(tmp_path))
    try:
        connect(mcp, e)
        r = mcp.tool(transport + '_send_input', dict(session_id='lossless', input_text='C',
                     prompt_pattern=r'P> ', max_wait_seconds=2))
        assert payload(r) == b'ANSWER[C]\r\nP> '
        assert payload(read(mcp, e)) == b'TAIL\r\n'
        assert payload(read(mcp, e, return_full_buffer=True)) == b''
    finally:
        mcp.close()
        e.close()


def test_a4_eof_pending_and_repeated_close(peer):
    mcp, e, _ = peer
    e.send(b'BEFORE-EOF\r\nP> AFTER-PROMPT\xff\x00\x11')
    e.eof()
    wait_bytes(mcp, e, len(e.sent))
    first = mcp.tool('telnet_read_session', dict(session_id='lossless', max_wait_seconds=2,
                                               prompt_pattern='P> '))
    assert payload(first) == b'BEFORE-EOF\r\nP> '
    second = read(mcp, e)
    assert payload(second) == b'AFTER-PROMPT\xff\x00\x11'
    assert payload(read(mcp, e)) == b''
    assert payload(mcp.tool('telnet_close_session', {'session_id': 'lossless'})) == b''
    assert payload(mcp.tool('telnet_close_session', {'session_id': 'lossless'})) == b''


def test_a5_screen_wait_are_nonconsuming(peer):
    mcp, e, _ = peer
    e.send(b'\x1b[2J\x1b[HSCREEN-ONE\r\n')
    wait_bytes(mcp, e, len(e.sent))
    assert 'SCREEN-ONE' in mcp.tool('terminal_get_screen', {'session_id': 'lossless'})['screen_text']
    e.send(b'SCREEN-TWO\r\n')
    r = mcp.tool('terminal_wait_for_change', dict(session_id='lossless', pattern='SCREEN-TWO',
                 settle_seconds=0.01, timeout_seconds=2))
    assert r['pattern_matched'] == 'SCREEN-TWO'
    sent = mcp.tool('telnet_send_input', dict(session_id='lossless', input_text='C',
                     prompt_pattern='P> ', max_wait_seconds=2))
    rest = read(mcp, e)
    assert payload(sent) + payload(rest) == bytes(e.sent)[len(b'READY> '):]


def test_a6_five_megabyte_unread(peer):
    mcp, e, path = peer
    data = (b'0123456789abcdef' * (5 * 1024 * 1024 // 16))
    started = time.monotonic()
    thread = threading.Thread(target=e.send, args=(data,))
    thread.start()
    wait_bytes(mcp, e, len(b'READY> ') + len(data), timeout=90)
    thread.join(5)
    assert not thread.is_alive()
    result = read(mcp, e)
    assert result['new_bytes_count'] == len(data)
    actual = payload(result)
    assert actual == data
    assert payload(read(mcp, e)) == b''
    assert Path(path, 'lossless.log.raw').read_bytes() == bytes(e.sent)
    print('A6', json.dumps(dict(bytes=len(actual), sha256=hashlib.sha256(actual).hexdigest(),
                               seconds=round(time.monotonic()-started,3), load=os.getloadavg())))


def test_a8_two_sessions_isolated(tmp_path):
    peers = [Endpoint(), Endpoint()]
    mcp = Mcp(str(tmp_path))
    try:
        initial = [connect(mcp, e, str(i)) for i, e in enumerate(peers)]
        for i, e in enumerate(peers):
            e.send((f'SESSION-{i}\r\n' * 1000).encode())
            e.eof()
        for i, e in enumerate(peers):
            wait_bytes(mcp, e, len(e.sent), str(i))
            r = read(mcp, e, str(i))
            assert payload(initial[i], 'initial_banner_raw_base64') + payload(r) == bytes(e.sent)
            assert payload(read(mcp, e, str(i))) == b''
    finally:
        mcp.close()
        for e in peers:
            e.close()


def test_utf8_incremental_and_arbitrary_bytes(peer):
    mcp, e, path = peer
    chunks = [b'\xe2', b'\x82', b'\xac\xff\x00\x11\xfe\r\n']
    texts, raw = [], bytearray()
    for chunk in chunks:
        e.send(chunk)
        wait_bytes(mcp, e, len(e.sent))
        r = read(mcp, e)
        texts.append(r['output'])
        raw.extend(payload(r))
    assert bytes(raw) == b''.join(chunks)
    assert ''.join(texts) == '€\ufffd\x00\x11\ufffd\r\n'
    assert Path(path, 'lossless.log.raw').read_bytes() == bytes(e.sent)


def test_close_delivers_unread_once(peer):
    mcp, e, _ = peer
    e.send(b'UNREAD-AT-CLOSE\xe2')
    wait_bytes(mcp, e, len(e.sent))
    r = mcp.tool('telnet_close_session', {'session_id': 'lossless'})
    assert payload(r) == b'UNREAD-AT-CLOSE\xe2'
    assert r['output'] == 'UNREAD-AT-CLOSE\ufffd'
    assert payload(mcp.tool('telnet_close_session', {'session_id': 'lossless'})) == b''


@pytest.mark.skipif(os.environ.get('LOSSLESS_LONG') != '1', reason='set LOSSLESS_LONG=1 for full 10k stress')
@pytest.mark.parametrize('transport', ['telnet', 'serial'])
def test_a1_a7_numbered_stream(transport, tmp_path):
    e = Endpoint(transport)
    mcp = Mcp(str(tmp_path))
    collected = bytearray()
    failures = []
    started = time.monotonic()
    load_start = os.getloadavg()
    try:
        r = connect(mcp, e)
        collected.extend(payload(r, 'initial_banner_raw_base64'))
        def numbered():
            rng = random.Random(80010)
            try:
                for n in range(1, 10001):
                    time.sleep(rng.uniform(0, 0.05))
                    e.send(f'LINE{n:05d}\r\n'.encode())
            except BaseException as ex:
                failures.append(repr(ex))
        sender = threading.Thread(target=numbered)
        sender.start()
        rng = random.Random(80020)
        calls = 0
        evidence_path = Path(tmp_path, f'{transport}-results.jsonl')
        with evidence_path.open('w') as evidence:
            while sender.is_alive():
                mode = rng.randrange(3)
                if mode == 0:
                    result = mcp.tool(transport + '_send_input', dict(session_id='lossless',
                         input_text=f'C{calls:05d}', max_wait_seconds=rng.uniform(0.005, 0.09),
                         prompt_pattern='P> '))
                else:
                    args = dict(session_id='lossless', max_wait_seconds=rng.uniform(0.005, 0.09))
                    if mode == 1:
                        args['idle_timeout'] = rng.uniform(0.005, 0.04)
                    else:
                        args['prompt_pattern'] = 'P> '
                    result = mcp.tool(transport + '_read_session', args)
                collected.extend(payload(result))
                evidence.write(json.dumps(result) + '\n')
                calls += 1
                if calls % 50 == 0:
                    mcp.tool('terminal_get_screen', dict(session_id='lossless'))
                time.sleep(rng.uniform(0, 0.025))
        sender.join()
        assert not failures
        # Let the command responder finish, then close after all sent bytes ingested.
        time.sleep(0.2)
        wait_bytes(mcp, e, len(e.sent))
        final = mcp.tool(transport + '_close_session', dict(session_id='lossless'))
        collected.extend(payload(final))
        assert bytes(collected) == bytes(e.sent)
        for n in range(1, 10001):
            assert collected.count(f'LINE{n:05d}\r\n'.encode()) == 1
        expected_sha = hashlib.sha256(e.sent).hexdigest()
        assert hashlib.sha256(collected).hexdigest() == expected_sha
        assert Path(tmp_path, 'lossless.log.raw').read_bytes() == bytes(e.sent)
        Path(tmp_path, f'{transport}-sent.bin').write_bytes(e.sent)
        Path(tmp_path, f'{transport}-returned.bin').write_bytes(collected)
        print('A1/A7', json.dumps(dict(transport=transport, lines=10000, calls=calls,
              bytes=len(collected), sent_sha256=expected_sha,
              returned_sha256=hashlib.sha256(collected).hexdigest(),
              seconds=round(time.monotonic()-started,3), load_start=load_start,
              load_end=os.getloadavg(), evidence=str(evidence_path))))
    finally:
        mcp.close()
        e.close()


def test_cancel_batch_after_eof_restores_all_unreturned_bytes():
    """The call dequeued its first response, then is cancelled in a batch delay."""
    import asyncio
    from unittest.mock import MagicMock
    from chuk_mcp_telnet_client import tools

    async def run():
        sid = 'cancel-eof'
        session = tools.TelnetSession(MagicMock(), 'localhost', 0, time.time(), sid)
        tools._session_store._sessions[sid] = session
        def write(data):
            tools._ingest_chunk(session, b'FINAL> EOF-TAIL', time.time())
            session.is_active = False
        session.telnet.write.side_effect = write
        task = asyncio.create_task(tools.telnet_client_tool(
            'localhost', 0, ['C1', 'C2'], telnet_session_id=sid,
            prompt_pattern='FINAL> ', command_delay=30, enable_logging=False,
        ))
        for _ in range(100):
            if session.transaction_bytes:
                break
            await asyncio.sleep(0.01)
        assert session.transaction_bytes == b'FINAL> '
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await tools.telnet_read_session(sid, max_wait_seconds=0)
        assert base64.b64decode(result.raw_base64) == b'FINAL> EOF-TAIL'
        assert (await tools.telnet_read_session(sid, max_wait_seconds=0)).output == ''
        tools._session_store._sessions.pop(sid)
    asyncio.run(run())


def test_cancel_close_retains_session_and_pending(monkeypatch):
    import asyncio
    from unittest.mock import MagicMock
    from chuk_mcp_telnet_client import tools

    async def run():
        sid = 'cancel-close'
        session = tools.TelnetSession(MagicMock(), 'localhost', 0, time.time(), sid)
        tools._session_store._sessions[sid] = session
        tools._ingest_chunk(session, b'KEEP-ON-CANCEL', time.time())
        entered, release = threading.Event(), threading.Event()
        def stopping(s):
            entered.set()
            assert release.wait(5)
            s.is_active = False
        monkeypatch.setattr(tools, '_stop_session_io', stopping)
        task = asyncio.create_task(tools.telnet_close_session(sid))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await tools._session_store.get(sid) is session
        release.set()
        result = await tools.telnet_read_session(sid, max_wait_seconds=0)
        assert base64.b64decode(result.raw_base64) == b'KEEP-ON-CANCEL'
        tools._session_store._sessions.pop(sid)
    asyncio.run(run())


def test_concurrent_consumers_never_repeat():
    import asyncio
    from unittest.mock import MagicMock
    from chuk_mcp_telnet_client import tools

    async def run():
        sid = 'concurrent-consumers'
        session = tools.TelnetSession(MagicMock(), 'localhost', 0, time.time(), sid)
        tools._session_store._sessions[sid] = session
        tools._ingest_chunk(session, b'ONLY-ONCE', time.time())
        results = await asyncio.gather(*(tools.telnet_read_session(sid, max_wait_seconds=0.01)
                                        for _ in range(8)))
        assert b''.join(base64.b64decode(r.raw_base64) for r in results) == b'ONLY-ONCE'
        tools._session_store._sessions.pop(sid)
    asyncio.run(run())


def test_close_joins_inflight_ingest(monkeypatch):
    import asyncio
    from chuk_mcp_telnet_client import tools

    async def run():
        e = Endpoint()
        try:
            result = await tools.telnet_client_tool('127.0.0.1', e.port, [],
                       telnet_session_id='inflight', response_wait=0.3, enable_logging=False)
            entered, release = threading.Event(), threading.Event()
            original = tools._ingest_chunk
            def ingest(s, data, arrived_at):
                if data == b'IN-FLIGHT':
                    entered.set()
                    assert release.wait(5)
                original(s, data, arrived_at)
            monkeypatch.setattr(tools, '_ingest_chunk', ingest)
            e.send(b'IN-FLIGHT')
            while not entered.is_set():
                await asyncio.sleep(0.01)
            closing = asyncio.create_task(tools.telnet_close_session('inflight'))
            await asyncio.sleep(0.1)
            assert not closing.done()
            release.set()
            final = await closing
            assert base64.b64decode(final.raw_base64) == b'IN-FLIGHT'
        finally:
            e.close()
    asyncio.run(run())


def test_attach_logging_to_existing_session(tmp_path):
    e = Endpoint()
    mcp = Mcp(str(tmp_path))
    try:
        initial = mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[],
                           telnet_session_id='attach', response_wait=0.3, enable_logging=False))
        assert payload(initial, 'initial_banner_raw_base64') == b'READY> '
        # Same commandless calls used to attach logging and turn timestamps on.
        for _ in range(2):
            mcp.tool('telnet_client', dict(host='127.0.0.1', port=e.port, commands=[],
                     telnet_session_id='attach', timestamp_chunks=True))
        e.send(b'SUBSEQUENT\r\n')
        wait_bytes(mcp, e, len(e.sent), 'attach')
        r = read(mcp, e, 'attach')
        assert payload(r) == b'SUBSEQUENT\r\n'
        log = Path(tmp_path, 'attach.log').read_text()
        assert log.count('SESSION LOG:') == 1
        assert 'READY>' not in log
        assert Path(tmp_path, 'attach.log.raw').read_bytes() == b'SUBSEQUENT\r\n'
    finally:
        mcp.close()
        e.close()


def test_input_is_never_logged(peer):
    mcp, e, path = peer
    # A raw input with no newline elicits no machine echo/response.
    secret = 'synthetic-private-input'
    mcp.tool('telnet_send_input', dict(session_id='lossless', input_text=secret,
                                     raw=True, max_wait_seconds=0.05))
    assert secret not in Path(path, 'lossless.log').read_text()
    assert secret.encode() not in Path(path, 'lossless.log.raw').read_bytes()


class RFC2217Endpoint:
    """Real pyserial RFC2217 negotiation over a local TCP socket."""
    transport = 'serial'

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = f'rfc2217://127.0.0.1:{self.listener.getsockname()[1]}'
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.sent = bytearray()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def write(self, data):
        with self.lock:
            self.conn.sendall(data)

    def _serve(self):
        import serial
        import serial.rfc2217
        self.conn, _ = self.listener.accept()
        self.device = serial.serial_for_url('loop://', timeout=0.1)
        self.manager = serial.rfc2217.PortManager(self.device, self)
        self.ready.set()
        buf = b''
        try:
            while not self.stop.is_set():
                if not select.select([self.conn], [], [], 0.1)[0]:
                    continue
                data = self.conn.recv(4096)
                if not data:
                    break
                buf += b''.join(self.manager.filter(data))
                while b'\n' in buf:
                    line, _, buf = buf.partition(b'\n')
                    self.send(b'ANSWER[' + line.rstrip(b'\r') + b']\r\nP> TAIL\r\n')
        except OSError:
            pass

    def send(self, data):
        assert self.ready.wait(5)
        with self.lock:
            self.conn.sendall(b''.join(self.manager.escape(data)))
            self.sent.extend(data)

    def eof(self):
        self.stop.set()
        self.conn.shutdown(socket.SHUT_WR)

    def close(self):
        self.stop.set()
        self.conn.close()
        self.listener.close()
        self.thread.join(1)
        self.device.close()


def test_rfc2217_prompt_tail_arbitrary_bytes_and_eof(tmp_path):
    e = RFC2217Endpoint()
    mcp = Mcp(str(tmp_path))
    try:
        r = connect(mcp, e)
        initial = payload(r, 'initial_banner_raw_base64')
        assert initial == b''
        e.send(b'OLDER\x00\x11\xff\xfe\xe2\x82\xac\r\n')
        wait_bytes(mcp, e, len(e.sent))
        response = mcp.tool('serial_send_input', dict(session_id='lossless', input_text='C',
                            prompt_pattern='P> ', max_wait_seconds=2))
        assert payload(response) == b'OLDER\x00\x11\xff\xfe\xe2\x82\xac\r\nANSWER[C]\r\nP> '
        assert payload(read(mcp, e)) == b'TAIL\r\n'
        # Send a burst immediately followed by TCP EOF. pyserial's own queue
        # must remain drainable even when its protocol reader has already died.
        burst = bytes(range(256)) * 1000
        e.send(burst)
        e.eof()
        wait_bytes(mcp, e, len(e.sent))
        final = read(mcp, e)
        assert payload(final) == burst
        assert payload(read(mcp, e)) == b''
        assert Path(tmp_path, 'lossless.log.raw').read_bytes() == bytes(e.sent)
        print('RFC2217', json.dumps(dict(bytes=len(e.sent), sha256=hashlib.sha256(e.sent).hexdigest())))
    finally:
        mcp.close()
        e.close()
