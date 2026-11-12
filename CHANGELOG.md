# Changelog

## 0.8.0

Unread Telnet and serial output is one byte FIFO. Sends include older unread
output first, prompt matches leave their tail pending, and EOF/close preserve
final bytes. Screen tools never consume the stream. Concurrent stream calls are
serialized; cancelled calls restore unreturned output, including command batches.

Stream replies add base64 fields for exact binary payloads and retain readable
incremental UTF-8 text. Telnet payload preserves NUL, XON and escaped IAC. Serial
RFC2217 EOF drains pyserial's queue after its protocol reader exits. Telnet reset recovery returns already-cooked payload,
and a failed negotiation reply cannot discard following payload. There is no
pending-size truncation or lifetime history copy.

Logging appends each received payload once, plus an exact `.log.raw` sidecar, and
never logs supplied input. Logging can attach to an existing session without
replaying its banner. Explicit close replies carry final unread output.

### Migration

* Command echoes are no longer stripped from machine output.
* `return_full_buffer=True` reads unread bytes without replaying delivered history.
* Screen-only inspection leaves output unread; later LOGOUT receives that output
  before logout text. Draining the stream first preserves the no-duplicate intent
  of the earlier M1L fix.
* An ended session must be explicitly closed before reusing its ID.
* A session transcript consists of `.log` and `.log.raw`, with only received
  machine bytes in the latter. Supplied commands and passwords are not included.

See the README for delivery boundaries and the qualification report for evidence.
