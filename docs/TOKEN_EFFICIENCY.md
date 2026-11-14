# Token efficiency of tool results (0.9.0)

Operator request, 14-NOV-2026: make the Terminal MCP server more token efficient
(`raw_base64` only on request; `annotated_text` only when it differs). Two further
changes in the same spirit were added by the launching session. Each is its own
commit on branch `tokeff-v090`, so any can be dropped.

## Method

`tests/measure_token_efficiency.py` drives the real stdio MCP server of a checkout
against a local fake Telnet host and a PTY serial peer (no real machine). It sends a
`SHOW SYSTEM`-sized reply (11 lines, ~830 bytes) and a 24x80 VT100 screen (12 text
lines, cursor on row 22), and records the **bytes of the result text the MCP client
receives** (the server pretty-prints JSON with indent 2; that text is what costs
tokens; roughly 4 bytes per token). Run once per version with that version's
interpreter. Host load average was 0.9 to 1.6 during the runs (another test run was
in the background part of the time); sizes do not depend on timing.

Columns: size at 0.8.0; after commit (a+b) the operator's two changes; after (c)
automatic raw for inexact text; after (d) empty fields, `note`, `server_version`; after
(e) screen trimming, which is 0.9.0; the saving; 0.9.0 when the call passes
`include_raw: true`; and 0.9.0 if the server sent compact JSON (not implemented,
see below).

| Result (fake host, `SHOW SYSTEM`-sized replies) | 0.8.0 | a+b | +c | +d | +e = 0.9.0 | saved | 0.9.0 with `include_raw` | 0.9.0 compact JSON |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| telnet_client (connect + 1 command) | 2761 | 1373 | 1373 | 1351 | 1351 | 51% | 2739 | 1281 |
| telnet_client (existing session, 1 command) | 2708 | 1348 | 1348 | 1302 | 1302 | 52% | 2662 | 1236 |
| telnet_send_input | 2484 | 1196 | 1196 | 1170 | 1170 | 53% | 2458 | 1145 |
| telnet_read_session (nothing new) | 302 | 282 | 282 | 256 | 256 | 15% | 276 | 219 |
| telnet_read_session (one SHOW SYSTEM waiting) | 2549 | 1261 | 1261 | 1235 | 1235 | 52% | 2523 | 1198 |
| telnet_send_input (reply has invalid UTF-8) | 300 | 252 | 350 | 324 | 324 | −8% (grows) | 274 | 291 |
| terminal_get_screen (24x80, 12 lines used, one reverse-video line) | 969 | 969 | 969 | 940 | 932 | 4% | 932 | 883 |
| terminal_wait_for_change (screen unchanged) | 1113 | 1113 | 1113 | 1057 | 1049 | 6% | 1049 | 980 |
| terminal_send_key (ENTER) | 1009 | 1009 | 1009 | 980 | 972 | 4% | 972 | 931 |
| terminal_get_screen (24x80, 12 lines used, plain) | 963 | 577 | 577 | 521 | 517 | 46% | 517 | 484 |
| telnet_close_session | 113 | 93 | 93 | 77 | 77 | 32% | 77 | 68 |
| serial_client (connect + 1 command) | 2713 | 1353 | 1353 | 1307 | 1307 | 52% | 2667 | 1241 |
| serial_send_input | 2484 | 1196 | 1196 | 1170 | 1170 | 53% | 2458 | 1145 |
| serial_read_session (nothing new) | 307 | 287 | 287 | 261 | 261 | 15% | 281 | 224 |
| serial_close_session | 113 | 93 | 93 | 77 | 77 | 32% | 77 | 68 |
| list_sessions (none open) | 179 | 179 | 179 | 73 | 73 | 59% | 73 | 60 |

Reading it:

* The stream tools save about half: `raw_base64` was a base64 copy (4/3 the size) of
  text that is already in the result. With `include_raw: true` the size is back to the
  0.8.0 level, a little below because of (d).
* The invalid-UTF-8 reply grows 24 bytes against 0.8.0 (the automatic `raw_base64`
  and `raw_included_reason` of (c)); (a) alone would have shrunk it to 252.
* Screens: the plain screen saves 46% (`annotated_text` was a duplicate of
  `screen_text`). A screen with a reverse-video line keeps its `annotated_text`, so it
  saves only 4% to 6%. Trailing blank lines (e) save little in this sample because the
  prompt is on row 22; a screen whose text ends higher saves one `\n` per cut line.
* The big sizes are the host's own text, which this change does not touch.

## What each commit does

| Commit | Change |
| :--- | :--- |
| a+b | `include_raw` (default false) on every stream tool; `*_raw_base64` omitted without it. `annotated_text` omitted when equal to `screen_text` (the change detector of `terminal_wait_for_change` still compares the full annotated text). |
| c | Lossless guarantee: `raw_base64` returned anyway, with `raw_included_reason: "invalid_or_split_utf8"`, when `text.encode("utf-8") != bytes` for that delivery: invalid UTF-8 (each bad byte became U+FFFD), or a UTF-8 character split across two deliveries (the first text lacks the incomplete tail, the next text holds a character whose first bytes are in the previous raw). Per command response, banner, final output, read, send, close, break. `.log.raw` still has every byte (tested with include_raw off). |
| d | Fields left out when null/empty/default: `prompt_matched`, `log_file`, `pattern_matched`, `cursor_char`, `highlighted_lines`, `initial_banner`, `final_output`, a close reply's `output`. `server_version` kept only in `telnet_client`, `serial_client`, `list_sessions`. Constant `note` of `list_sessions` dropped. |
| e | Screens drop trailing blank lines unless `full_screen: true` (or `include_raw: true`). |
| 0.9.0 | Version strings, README, CHANGELOG, this file. |

## Differences from the request, for the reviewer

* **`note`:** the request named `TelnetSendInputOutput.note`; that model has none. The
  constant `note` string is in `list_sessions` (`SessionListResponse`), and that is
  what was removed. `list_sessions` also lost the per-session `server_version`
  (it repeated the top-level one on every entry).
* **Trailing spaces on each line:** 0.8.0 already right-trimmed every screen line
  (`line.rstrip()`), so (e) only adds the trailing blank lines. `full_screen` keeps
  the per-line trim (padding lines with spaces would only cost tokens).
* **`include_raw` on screen tools:** accepted and means `full_screen`, as the request
  words it ("unless include_raw or a new full_screen"); screen results hold no bytes.
* **Booleans are always sent.** "At its default" was not applied to booleans
  (`command_completed`, `cursor_visible`, `session_active`): an absent `true` is easy
  to misread, and each is a few bytes. `output`, `response`, `screen_text` and the
  counts are always present, so an empty reply still says so.
* **Empty `prompt_matched`:** a pattern that matches the empty string reports no
  `prompt_matched` (it is omitted like null).
* `close` with `include_raw: true` on a missing session returns `raw_base64: ""`,
  as 0.8.0 did.

## Not done (candidates, each needs the operator's word)

* **Compact JSON.** `chuk_mcp_server` serializes results with `indent=2`. The last column
  shows 5% to 15% less (more on small results) with compact separators. It would mean
  returning a JSON string from each tool instead of a model. Not implemented.
* `terminal_send_key` repeats the key in three fields (`key_sent`, `bytes_sent`,
  `message`).
* `terminal_send_key` and `terminal_wait_for_change` return the whole screen every
  time; a "changed rows only" form would save more than anything above for slow
  screens, but changes the contract.

## Tests

Updated, because they asserted `raw_base64` is present by default: `tests/test_lossless_stream.py`
(its MCP client now passes `include_raw: true` on stream calls, and its seven in-process
calls that read `.raw_base64` pass it) and `tests/test_utf8_prompt_offset.py` (uses that client).
`tests/test_m1_live_reader.py::test_wait_for_change_pattern` asserted `pattern_matched is None`; it now asserts the field is absent.
`tests/test_standalone_serial.py` expects `server_version == "0.9.0"`.
New: `tests/test_token_efficiency.py` (every change, the automatic raw for undecodable
and split bytes, byte-exact round trip over all byte values on TCP and PTY with
`include_raw`, raw transcript unchanged).

## Changes to this task accepted from the launching session

None so far.
