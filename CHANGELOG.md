# Changelog

## 0.8.0

Unread Telnet and serial output is one byte FIFO. Sends include older unread
output first, prompt matches leave their tail pending, and EOF/close preserve
final bytes. Screen tools never consume the stream. Concurrent stream calls are
serialized; cancelled calls restore unreturned output, including command batches.
Prompt search maps the pre-send byte boundary in the full reversible text, so
a split UTF-8 character cannot hide a following prompt. Complete older prompts
remain excluded, and output after the new matched prompt remains pending.

Stream replies add base64 fields for exact binary payloads and retain readable
incremental UTF-8 text. Broad regex prompt metadata replaces invalid UTF-8 for
JSON-safe display without changing matched byte offsets or payload delivery. Telnet payload preserves NUL, XON and escaped IAC. Serial
RFC2217 EOF drains pyserial's queue after its protocol reader exits. Telnet reset recovery returns already-cooked payload,
and a failed negotiation reply cannot discard following payload. There is no
pending-size truncation or lifetime history copy.

Logging appends each received payload once, plus an exact `.log.raw` sidecar, and
does not separately append supplied input arguments. Host-echoed commands remain
in this received-output audit trail; non-echoed input, including normal password
entries, is absent. A host-generated echo of a secret is still received payload.
Logging can attach to an existing session without replaying its banner. Explicit
close replies carry final unread output.

### Migration

* **Visible behavior change:** command echoes are no longer stripped from machine
  output. They appear in replies and received-output logs when the host echoes
  them; non-echoed input is not independently recorded.
* `return_full_buffer=True` reads unread bytes without replaying delivered history.
* Screen-only inspection leaves output unread; later LOGOUT receives that output
  before logout text. Draining the stream first preserves the no-duplicate intent
  of the earlier M1L fix.
* An ended session must be explicitly closed before reusing its ID.
* A session transcript consists of `.log` and `.log.raw`, with only received
  machine bytes in the latter. This stores a second payload copy; budget roughly
  twice the payload disk space plus text-log metadata and decoding overhead.

See the README for delivery boundaries and the qualification report for evidence.
