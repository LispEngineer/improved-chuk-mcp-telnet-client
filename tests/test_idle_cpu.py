"""v0.9.2: a waiting call costs no CPU while nothing arrives, and still wakes at once.

(a) unit tests of _wait_for_output's incremental search, with a real session object fed
    through _ingest_chunk (no sockets);
(b) the operator's acceptance test: a server process holding a session open on a silent
    fake telnet server / pty peer uses under 1% of one core over 30 s, with no earlier
    output and with 300 KB of earlier output pending;
(c) latency: a prompt that arrives after 5 s of silence is returned within 50 ms.
"""

import asyncio
import os
import pty
import random
import re
import socket
import threading
import time
import tty

import pytest

import chuk_mcp_telnet_client.tools as tools
from chuk_mcp_telnet_client.tools import (
    TelnetSession,
    _ingest_chunk,
    _wait_for_output,
    serial_client_tool,
    serial_close_session,
    serial_read_session,
    telnet_client_tool,
    telnet_close_session,
    telnet_read_session,
)
from tests.idle_cpu_harness import prelude_bytes, run_case

USER_PROMPT = r"(?:[A-Za-z0-9]+::)?USER1\$ $"


def make_session():
    return TelnetSession(telnet=None, host="fake", port=0, created_at=time.time(), session_id="unit")


class CountingPattern:
    """Wraps a compiled pattern and records the `pos` of every search."""

    def __init__(self, pattern, log):
        self._pattern, self._log = pattern, log

    def search(self, text, pos=0, *rest):
        self._log.append((pos, len(text)))
        return self._pattern.search(text, pos, *rest)


@pytest.fixture
def searches(monkeypatch):
    log = []
    real_re = re

    class Shim:
        def compile(self, pattern, *args):
            return CountingPattern(real_re.compile(pattern, *args), log)

        def __getattr__(self, name):
            return getattr(real_re, name)

    monkeypatch.setattr(tools, "re", Shim())
    return log


def feed_later(session, chunks_with_delay):
    def run():
        for delay, data in chunks_with_delay:
            time.sleep(delay)
            _ingest_chunk(session, data, time.time())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# (a) ----------------------------------------------------------------------------------------

def test_no_search_and_no_decode_without_new_data(searches, monkeypatch):
    monkeypatch.setattr(tools.TerminalDefaults, "WAIT_HEARTBEAT_SECONDS", 0.05)
    session = make_session()
    _ingest_chunk(session, prelude_bytes(50_000), time.time())

    async def go():
        t0 = time.monotonic()
        out = await _wait_for_output(session, 0, max_wait_seconds=1.0, prompt_pattern=USER_PROMPT)
        return out, time.monotonic() - t0

    (text, completed, matched), waited = asyncio.run(go())
    assert completed is False and matched is None and len(text) >= 50_000
    assert waited >= 0.95
    # ~20 heartbeat wake-ups, one search: the first look. (0.9.1 searched 50 times a second.)
    assert len(searches) == 1


def test_new_data_triggers_one_incremental_search_from_the_lookback(searches, monkeypatch):
    monkeypatch.setattr(tools.TerminalDefaults, "PROMPT_LOOKBACK_CHARS", 100)
    session = make_session()
    _ingest_chunk(session, prelude_bytes(20_000), time.time())
    first_len = len(session.pending_output)
    feed_later(session, [(0.2, b"more output\r\n"), (0.2, b"and more\r\n")])

    async def go():
        return await _wait_for_output(session, 0, max_wait_seconds=0.8, prompt_pattern=USER_PROMPT)

    asyncio.run(go())
    assert len(searches) == 3
    assert searches[0][0] == 0
    assert searches[1][0] == first_len - 100          # resumed 100 characters before the end
    assert searches[2][0] == first_len + len(b"more output\r\n") - 100


def test_prompt_split_across_chunks_is_found(searches):
    session = make_session()
    _ingest_chunk(session, prelude_bytes(10_000) + b"VAX96::USE", time.time())
    feed_later(session, [(0.2, b"R1$ ")])
    text, completed, matched = asyncio.run(
        _wait_for_output(session, 0, max_wait_seconds=3.0, prompt_pattern=USER_PROMPT))
    assert completed and matched == "VAX96::USER1$ "
    assert text.endswith("VAX96::USER1$ ")
    assert len(session.pending_output) == 0


def test_prompt_longer_than_the_lookback_is_a_documented_limit(monkeypatch):
    pattern = r"X{40}!"

    def run(lookback):
        monkeypatch.setattr(tools.TerminalDefaults, "PROMPT_LOOKBACK_CHARS", lookback)
        session = make_session()
        _ingest_chunk(session, b"X" * 39, time.time())
        feed_later(session, [(0.2, b"X!")])
        return asyncio.run(_wait_for_output(session, 0, max_wait_seconds=0.8, prompt_pattern=pattern))

    assert run(16)[1] is False           # a 41-character match, resumed 16 back: missed
    assert run(4096)[1] is True          # the default lookback finds it


def test_search_never_starts_before_start_index():
    session = make_session()
    _ingest_chunk(session, b"VAX96::USER1$ \r\nold output\r\n", time.time())
    start = len(session.pending_output)
    feed_later(session, [(0.2, b"new output, no prompt yet\r\n")])
    text, completed, matched = asyncio.run(
        _wait_for_output(session, start, max_wait_seconds=0.6, prompt_pattern=USER_PROMPT))
    assert completed is False and matched is None     # the old prompt is before start_index
    assert text.startswith("VAX96::USER1$ ") and text.endswith("no prompt yet\r\n")


def test_utf8_character_split_at_a_chunk_boundary():
    session = make_session()
    euro = "€".encode()
    _ingest_chunk(session, b"price " + euro[:2], time.time())
    feed_later(session, [(0.2, euro[2:] + b" \xe2\x80\x94 done\r\nUSER1$ ")])
    text, completed, matched = asyncio.run(
        _wait_for_output(session, 0, max_wait_seconds=3.0, prompt_pattern=USER_PROMPT))
    assert completed and matched == "USER1$ "
    assert text == "price € — done\r\nUSER1$ "


def _reference(chunks, pattern, start_index):
    """0.9.1 semantics: after every chunk, search all the pending text from start_index."""
    compiled = re.compile(pattern)
    raw = b""
    for chunk in chunks:
        raw += chunk
        text = raw.decode("utf-8", errors="surrogateescape")
        m = compiled.search(text, tools._prompt_text_offset(text, start_index))
        if m:
            length = len(text[:m.end()].encode("utf-8", errors="surrogateescape"))
            return raw[:length]
    return None


def test_same_answers_as_a_full_search_on_random_streams():
    rng = random.Random(9)
    words = [b"alpha ", b"BETA\r\n", b"$ ", "café € ".encode(), b"NODE::", b"USER1$", b"\xff\xfe ",
             b"gamma>\r\n", b"USER1$ ", b"\r\n$ "]
    for _ in range(25):
        stream = b"".join(rng.choice(words) for _ in range(rng.randint(3, 40)))
        cuts = sorted(rng.sample(range(1, len(stream)), min(len(stream) - 1, rng.randint(1, 8))))
        chunks = [stream[a:b] for a, b in zip([0] + cuts, cuts + [len(stream)])]
        pattern = rng.choice([USER_PROMPT, tools.TerminalDefaults.PROMPT_PATTERN])
        start_index = rng.choice([0, 0, rng.randint(0, 8)])
        session = make_session()
        _ingest_chunk(session, b"xx\xe2\x82"[:start_index], time.time()) if start_index else None
        start = len(session.pending_output)
        expected = _reference(([bytes(session.pending_output)] if start else []) + chunks, pattern, start)
        feed_later(session, [(0.03, c) for c in chunks])
        text, completed, matched = asyncio.run(
            _wait_for_output(session, start, max_wait_seconds=1.0 + 0.03 * len(chunks), prompt_pattern=pattern))
        if expected is None:
            assert completed is False, (chunks, pattern)
        else:
            # Delivered: every pending byte up to the end of the prompt, as a full search says.
            assert completed is True and text.raw == expected, (chunks, pattern, start)


# (b) the operator's acceptance test -------------------------------------------------------

IDLE_WINDOW = float(os.environ.get("IDLE_WINDOW", "30"))


@pytest.mark.parametrize("kind", ["telnet", "serial"])
@pytest.mark.parametrize("prelude", [0, 300_000], ids=["no-earlier-output", "300KB-pending"])
def test_idle_cpu_under_one_percent_of_a_core(kind, prelude):
    r = run_case(kind, prelude_bytes(prelude), window=IDLE_WINDOW)
    print(f"\n{kind} prelude={prelude}: {r['percent_of_core']:.2f}% of a core "
          f"({r['cpu_seconds']:.3f} s in {r['wall_seconds']:.1f} s), load1={r['load1']:.2f}")
    assert r["percent_of_core"] < 1.0, r


# (c) latency --------------------------------------------------------------------------------

def test_prompt_after_five_seconds_of_silence_returns_within_50_ms_telnet(tmp_path):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    sent_at = []

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"Welcome\r\n")
        time.sleep(5.5)
        sent_at.append(time.monotonic())
        conn.sendall(b"\r\nVAX96::USER1$ ")
        time.sleep(1)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()

    async def go():
        await telnet_client_tool(host="127.0.0.1", port=srv.getsockname()[1], commands=[],
                                 telnet_session_id="lat_t", log_dir=str(tmp_path), response_wait=0.5)
        res = await telnet_read_session(session_id="lat_t", max_wait_seconds=30, prompt_pattern=USER_PROMPT)
        done = time.monotonic()
        await telnet_close_session("lat_t")
        return res, done

    res, done = asyncio.run(go())
    assert res.command_completed and res.prompt_matched == "VAX96::USER1$ "
    assert done - sent_at[0] < 0.05, done - sent_at[0]


def test_prompt_after_five_seconds_of_silence_returns_within_50_ms_serial(tmp_path):
    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    sent_at = []

    def serve():
        time.sleep(5.5)
        sent_at.append(time.monotonic())
        os.write(master, b"\r\nVAX96::USER1$ ")

    threading.Thread(target=serve, daemon=True).start()

    async def go():
        await serial_client_tool(port=os.ttyname(slave), commands=[], serial_session_id="lat_s",
                                 log_dir=str(tmp_path), response_wait=0.5)
        res = await serial_read_session(session_id="lat_s", max_wait_seconds=30, prompt_pattern=USER_PROMPT)
        done = time.monotonic()
        await serial_close_session("lat_s")
        return res, done

    try:
        res, done = asyncio.run(go())
    finally:
        os.close(master)
        os.close(slave)
    assert res.command_completed and res.prompt_matched == "VAX96::USER1$ "
    assert done - sent_at[0] < 0.05, done - sent_at[0]
