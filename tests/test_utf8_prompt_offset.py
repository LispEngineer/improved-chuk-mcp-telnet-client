"""Byte-boundary prompt search through the actual stdio TCP and PTY tools."""
import base64
import hashlib
import json
import os
from pathlib import Path
import select

import pytest

from test_m1_live_reader import Mcp
from test_lossless_stream import Endpoint, connect, payload, read, wait_bytes


CASES = [
    ('two-byte-one', b'\xc2', b'\xa3'),
    ('euro-one', b'\xe2', b'\x82\xac'),
    ('euro-two', b'\xe2\x82', b'\xac'),
    ('emoji-three', b'\xf0\x9f\x98', b'\x80'),
    ('malformed-sequence', b'\xe2\x82', b'X'),
    ('malformed-bytes', b'\xff\x80', b''),
    ('old-whole-prompt', b'RESPONSE> ', b''),
    ('old-prompt-euro-two', b'RESPONSE> \xe2\x82', b'\xac'),
]


class OffsetEndpoint(Endpoint):
    def __init__(self, transport, suffix):
        self.suffix = suffix
        super().__init__(transport)

    def _serve(self):
        if self.transport == 'telnet':
            self.conn, _ = self.listener.accept()
            self.conn.setblocking(True)
        self.ready.set()
        if self.transport == 'telnet':
            self.send(b'READY> ')
        buf = b''
        try:
            while not self.stop.is_set():
                fd = self.conn if self.transport == 'telnet' else self.master
                if not select.select([fd], [], [], 0.1)[0]:
                    continue
                data = self.conn.recv(4096) if self.transport == 'telnet' else os.read(self.master, 4096)
                if not data:
                    break
                buf += data
                if b'\n' in buf:
                    self.send(self.suffix)
                    buf = b''
        except OSError:
            pass


@pytest.mark.parametrize('transport', ['telnet', 'serial'])
@pytest.mark.parametrize('name,old_prefix,new_prefix', CASES, ids=[x[0] for x in CASES])
def test_prompt_start_byte_boundary(transport, name, old_prefix, new_prefix, tmp_path):
    suffix = new_prefix + b'RESPONSE> TAIL'
    e = OffsetEndpoint(transport, suffix)
    mcp = Mcp(str(tmp_path))
    try:
        opening = connect(mcp, e)
        delivered = bytearray(payload(opening, 'initial_banner_raw_base64'))
        e.send(old_prefix)
        wait_bytes(mcp, e, len(e.sent))
        first = mcp.tool(transport + '_send_input', dict(session_id='lossless', input_text='C',
                         prompt_pattern='RESPONSE> ', max_wait_seconds=0.2))
        following = read(mcp, e)
        final = mcp.tool(transport + '_close_session', dict(session_id='lossless'))
        delivered.extend(payload(first)); delivered.extend(payload(following)); delivered.extend(payload(final))
        sent_sha = hashlib.sha256(e.sent).hexdigest()
        returned_sha = hashlib.sha256(delivered).hexdigest()
        raw = Path(tmp_path, 'lossless.log.raw').read_bytes()
        evidence = dict(case=name, transport=transport, command_completed=first['command_completed'],
                        prompt_matched=first['prompt_matched'], first_raw_base64=first['raw_base64'],
                        next_raw_base64=following['raw_base64'], sent_bytes=len(e.sent),
                        returned_bytes=len(delivered), sent_sha256=sent_sha, returned_sha256=returned_sha,
                        byte_exact=bytes(delivered)==bytes(e.sent), raw_log_exact=raw==bytes(e.sent))
        Path(tmp_path, 'result.json').write_text(json.dumps(evidence, indent=2)+'\n')
        assert evidence['byte_exact'] and evidence['raw_log_exact']
        assert sent_sha == returned_sha
        assert first['command_completed'] is True, evidence
        assert first['prompt_matched'] == 'RESPONSE> '
        assert payload(first) == old_prefix + new_prefix + b'RESPONSE> '
        assert payload(following) == b'TAIL'
        assert payload(final) == b''
    finally:
        mcp.close(); e.close()
