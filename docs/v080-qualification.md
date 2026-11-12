# v0.8.0 local qualification

This is an isolated development candidate. No installed/shared tool, agent,
MCP configuration or remote machine was changed or contacted. Deployment and
real-world acceptance remain separate gates.

## Environment and reproduction

Python 3.12.15, Linux, pyserial 3.5, pyte 0.8.2 and the repository's locked
chuk-mcp-server 0.26.1. Scratch, backups, logs and the dedicated venv use verified
disk-backed storage at `$HOME/tmp/mud-telnet-v080`. Set `TMPDIR`, `TMP` and `TEMP`
to that path. Existing files were backed up with timestamped `cp -a` copies.

From the checkout, using that venv's Python:

```bash
VAX_LONG_TEST_DURATION=3 python -m pytest tests/ -q --durations=10
LOSSLESS_LONG=1 python -m pytest 'tests/test_lossless_stream.py::test_a1_a7_numbered_stream[telnet]' -q -s
LOSSLESS_LONG=1 python -m pytest 'tests/test_lossless_stream.py::test_a1_a7_numbered_stream[serial]' -q -s
VAX_LONG_TEST_DURATION=185 python -m pytest tests/test_timeout_3min.py -q -s
```

Long tests run the real checkout MCP server over stdio, as do the screen/M1L and
new lossless regressions. The existing manual `test_long_running.py` runner was
also executed through the local fake harness
`$HOME/tmp/mud-telnet-v080/run_manual_offline.py`; its module HOST/PORT were
explicitly replaced with an owned ephemeral localhost listener before execution.
No existing forwarded endpoint was used. That 45-second job passed in 46.22 s.

## Results

| Gate | Observed local result |
| --- | --- |
| A1 | Full 10,000-line fake TCP stream, seeded 0–50 ms delays and randomized send/prompt/idle/max-wait/read calls: exact FIFO payload and unique lines; 5,368 calls, 153,307 bytes, 253.53 s |
| A2 | Bytes unread before send appear first; an older unread prompt does not complete new input |
| A3 | TCP and PTY prompt match ending mid-chunk returns only through match; next read returns tail once |
| A4 | Pending output survives EOF; prompt tail and final data are returned once; repeated EOF reads/close do not replay |
| A5 | Mixed get-screen/wait/send/read retains the complete FIFO and correct rendered screen |
| A6 | 5,242,880 unread bytes returned in one read; exact count, payload and raw transcript SHA; roughly 12–13 s |
| A7 | Full 10,000-line PTY stream: exact FIFO payload and unique lines; 5,483 calls, 154,150 bytes, 253.66 s; PTY fixture used, no socat or system installation required |
| A8 | Two concurrent TCP sessions remain isolated and complete |
| A9 | Complete fast suite: **82 passed, 2 long stress tests skipped**, 109.82 s; those two opt-in tests passed separately; actual 185-second timeout test: **1 passed**, 186.79 s |

Two supplemental failure-boundary regressions also passed (2 tests, 0.28 s):
a failed send retains older unread output, and a busy close fails explicitly,
retains the session, then returns in-flight payload on retry. The full fast
suite above ran before these two test-only additions; together they cover all
84 current fast cases. No implementation changed for the additions.

Additional passing cases cover malformed/split UTF-8, NUL/XON/escaped IAC,
concurrent consumers, cancellation during a batch after EOF, cancelled close,
close joining an in-flight ingest, attaching logging to an existing session,
input privacy, and final close draining. A real local RFC2217 negotiation test
passed 256,034 bytes, including all byte values and a 256,000-byte burst followed
immediately by EOF. It exercises the pyserial receive-queue drain after the
protocol thread has died.

The table records completed final repeats against implementation commit
`441a5dd4a5753ff9f35f5024b2abd0646fc9dbfe`, after reset/negotiation recovery
was added. The preceding full fast suite also tested that implementation. The
185-second serial timeout and 45-second manual job passed before the Telnet-only
reset follow-up; their relevant serial/normal command paths did not change.
Earlier passing runs are retained separately. Seeded schedules are reproducible,
but OS scheduling changes command counts and interleaving, so each stress run
compares its own sent and returned byte oracle.

The built wheel was installed in a second venv and exercised from a scratch cwd,
with package imports verified under that venv's `site-packages`: TCP prompt tail,
PTY prompt tail and real RFC2217 EOF/binary payload checks all passed (3 cases).
The first follow-up-wheel smoke reused an earlier log directory and failed raw
log equality because reconnect correctly appends. That failed run is retained
as `wheel-smoke-441a5dd.log`; a fresh unique-directory rerun completed with exit 0
and three passes in `wheel-smoke-441a5dd-2.log`. A premature progress report of
the first run as passed was explicitly corrected before handoff.

Build hashes and exact build/source commits are recorded in the external artifact
manifest; no deployment is implied by a built artifact. Build command:

```bash
python -m build --no-isolation --outdir "$HOME/tmp/mud-telnet-v080/artifacts-final"
```

Build dependencies are installed only in the dedicated venv. Both wheel and
sdist are produced, and the wheel is built from that sdist by the build frontend.

## Byte evidence

| Stream | Sent SHA-256 = returned SHA-256 |
| --- | --- |
| A1 TCP | `2a9c6e80425d5199cd63bf3e3006f6b9ec562a79c0b4ab5b4594b8fec78fa916` |
| A7 PTY | `6d59e560e6dfe92233c5be426dbd8ff42b2c8133b9f8b687efee4cf0eaaac7cf` |
| A6 5 MiB | `18a42ab71ef74ce8c5016d928b7a858aace90effdc15fcf9ab18bdc250e9d945` |
| RFC2217 | `f2e2e159090daeb660ce593d51b92a5eb0dbf293a5378a755c9fb2196fbd07cf` |

Final stress runs started with host loads (1/5/15 minute averages)
7.25/8.38/9.14 and ended at 10.89/9.12/9.19 for both transports. This shared host was not idle.
Each stress report also records actual elapsed time, call count, payload count,
SHA and load. Sent/returned binary files, full returned JSONL and the exact raw
transcript are retained under the corresponding stress test directories.

Evidence directory: `$HOME/tmp/mud-telnet-v080/evidence/`. Important logs:
`baseline-m1l.log`, `baseline-loss.json`, `original-tests-on-candidate.log`,
`reset-fast.log`, `rfc2217.log`, `reset-all.log`, `failure-boundaries.log`,
`timeout-185.log`, `manual-offline.log`, `reset-frozen-tcp.log`,
`reset-frozen-pty.log`, and `wheel-smoke-441a5dd-2.log`. Earlier stress logs,
including the two corrected fixture failures, are retained too.

## Legacy test changes and initial failures

The original v0.7.3 M1L test passes on its preserved baseline (2 tests, 6.78 s).
The baseline loss probe confirms older unread output disappears and a prompt's
tail is incorrectly bundled into the first result, leaving no tail for the next
read. The original suite against the candidate recorded 51 passes and six
failures before an existing fixture cleanup deadlock was interrupted; this is
retained as evidence, not described as an unmodified-suite pass.

The two original M1L assertions expected unread SCREEN traffic to disappear on
LOGOUT after screen-only inspection. That conflicts with the required
nonconsuming-screen/FIFO contract. Four revised cases now verify both sides:
screen-only views retain all 40 unread lines before LOGOUT, while an explicit
stream drain first prevents those already-delivered lines from reappearing.
Both immediate and delayed EOF are covered, including the post-prompt tail.

Other legacy changes update version assertions, expect `.log` plus `.log.raw`,
and check actual received output instead of relying on logged input commands or
passwords. The Telnet fake's cleanup closes its active writers before awaiting
`server.wait_closed`; on Python 3.12, the reverse order deadlocks when a test
assertion fails. The manual runner now aggregates actual returned output instead
of requesting history replay, and uses the normal login-prompt pattern for its
login command batch.

Two fixture failures were retained and corrected: the first 5 MiB fake send used
a 0.1 s send timeout and stopped prematurely; the first PTY stress counted a
7-byte banner written before the serial port opened, which the driver's open
flush discarded. The corrected PTY peer emits no pre-open banner. Neither
failure is counted as a passing candidate gate.

## Boundaries and pending real-world acceptance

The README defines byte-delivery, cancellation, close and crash/reply-loss
boundaries. A further existing open-failure limitation is explicit: automatic
RFC2217-to-raw fallback creates a failed first connection before a session exists;
bytes emitted during failed negotiation/open cannot be recovered from pyserial's
failed opener. Session delivery guarantees start after successful transport open,
including payload then queued by the backend. No claim is made for failed opens.
The pyserial RFC2217 recovery adapter relies on the tested 3.5 receive queue;
backend-version changes require requalification.

B1–B7 are **pending**: USER1 periodic ticks with interleaved commands/idle gaps,
unsolicited broadcast, LOGOUT after screen reads, normalized 1 MiB TYPE transfer,
reconnect prompt answered NO, two ordinary MUD players completing the complete
quest with death/reward events in returned data, and at least ten minutes of
unsolicited idle output. No local fake or simulator result establishes any of
these gates. Independent review and owner-controlled deployment remain pending.
