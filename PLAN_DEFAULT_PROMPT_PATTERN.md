# Plan: Fix the Default `prompt_pattern` for VMS-Style Prompts

**Author**: Douglas P. Fields, Jr. (`symbolics@lisp.engineer`), drafted by
Claude Sonnet 5, 2026-09-12.

**Status**: PLANNING ONLY - not yet implemented. Captured here so the idea
isn't lost; do this "later" per the operator.

## 1. Purpose

`chuk_mcp_telnet_client`'s `telnet_client_tool` and `serial_client_tool`
both default `prompt_pattern` to `TerminalDefaults.PROMPT_PATTERN`
(`chuk_mcp_telnet_client/tools.py`), currently:

```python
PROMPT_PATTERN: str = r"(?m)(^\$ |Username: |Password: |>>> )"
```

This default does not match the prompts actually produced by our primary
target, OpenVMS (VAX96), outside of login (`Username:`/`Password:`) and the
console firmware (`>>> `). Two real gaps, both hit repeatedly during the
`vms-savage` project's Sonnet 5 telnet sessions:

1. **DCL prompts are customized with a node/user prefix.** VAX96's
   `SYLOGIN.COM` sets the prompt to `NODE::USERNAME$ ` (e.g.
   `VAX96::USER1$ `), not a bare `$ `. `^\$ ` requires the `$` to be the
   first character of a line, so it never matches - every
   `telnet_client_tool`/`serial_client_tool` call that relies on the
   default silently fails to detect command completion
   (`command_completed: false`, `prompt_matched: null`) and returns only
   after `max_wait_seconds` elapses, having executed only the first queued
   command (typically just login). The caller then has to fall back to
   `telnet_send_input` with an explicit `prompt_pattern` (e.g. `"\$ $"`)
   for every subsequent command - which works, but defeats the point of
   the batched `commands` list and has to be rediscovered/re-typed every
   session.
2. **VMS is full of nested subsystem/utility prompts ending in `>`, not
   `$`.** DCL's `RUN`/subsystem commands drop you into a different prompt
   entirely until you exit that facility - e.g. `TCPIP>` (the TCP/IP
   management utility), `MCL>`, and more generally things like `NCP>`,
   `AUTHORIZE>`, `SET HOST 0>`, `ANALYZE/SYSTEM>`, `PATCH>`, `EDT>`. None
   of these are covered by the current default at all, so any automation
   that shells out to one of these utilities has to hand-roll its own
   `prompt_pattern` from scratch, including rediscovering that VMS
   sub-prompts are just "some facility name, then `>`, then maybe a
   trailing space" - a genuinely generic, learnable shape.

## 2. Evidence from this session (vms-savage work)

Representative failure, `telnet_client` called with the default prompt
pattern against VAX96:

```json
{
  "responses": [
    {"command": "USER1", "response": "\r\n\rPassword: "},
    {"command": "user1pass", "response": "...VAX96::USER1$ "}
  ],
  "command_completed": false,
  "prompt_matched": "Password: "
}
```

Only 2 of 5 queued commands ran (login only); `SET DEFAULT [.SAVAGE]`,
`SET TERMINAL /ESCAPE`, and the `MMS` build command were never sent,
because the tool never saw `VAX96::USER1$ ` as a completed prompt and
gave up at `max_wait_seconds`. Recovery required three separate
`telnet_send_input` calls, each passing `prompt_pattern="\$ $"` by hand.
This is not a one-off - it happens on every fresh session against VAX96
and has been silently worked around this way for the whole project so
far, rather than fixed at the source.

## 3. Proposed new default

```python
PROMPT_PATTERN: str = (
    r"("
    r"(?m:^\$ )"                              # bare "$ " DCL prompt (stock, uncustomized SYLOGIN)
    r"|\$ ?$"                                 # "...$" / "...$ " at the true end of the buffer -
                                               # matches a customized DCL prompt of the form
                                               # "NODE::USER$ " regardless of the node/user prefix,
                                               # WITHOUT the (?m) scope, so $ anchors to the actual
                                               # end of everything received so far, not just any
                                               # line - i.e. "is the prompt the last thing we got"
    r"|(?m:^[A-Za-z][A-Za-z0-9_ /]*> ?$)"     # VMS subsystem/utility prompts: TCPIP>, MCL>, NCP>,
                                               # AUTHORIZE>, SET HOST 0>, ANALYZE/SYSTEM>, PATCH>...
                                               # "word chars/spaces/slash, then >, then maybe a space"
    r"|Username: "
    r"|Password: "
    r"|>>> "                                  # VAX/Alpha console firmware prompt (unchanged)
    r")"
)
```

Design notes:

* **Scoped inline flags, not one blanket `(?m)`.** Python's `re` supports
  `(?m:...)` to apply MULTILINE to just one alternative. The two
  line-start-anchored alternatives (`^\$ `, the subsystem-prompt one) need
  MULTILINE so they can match a prompt that reappears partway through an
  accumulated multi-command buffer, not just at byte 0. The `\$ ?$`
  alternative deliberately does NOT get MULTILINE: without it, `$` means
  "end of the whole string" (or just before a lone trailing newline),
  which is exactly the semantic we want for "has the real, final prompt
  arrived" - not "does some line anywhere in the buffer happen to end in
  `$`". `_wait_for_output()` (`tools.py`) re-`search()`s the whole
  accumulated-so-far buffer on every 0.1s poll, so anchoring to the true
  end of that buffer is the correct way to detect "nothing more is
  coming."
* **Why not fold the node/user prefix into the regex explicitly** (e.g.
  `^\S+::\S+\$ `)? Because the prefix format is a VMS/DCL convention set
  by `SYLOGIN.COM`, not a protocol guarantee - it can be turned off, look
  different per site, or be entirely absent (bare `$ `, already covered).
  Anchoring only on the trailing `$` (optionally followed by one space) at
  the true end of the buffer is both simpler and more general.
* **Why allow a space and `/` in the subsystem-prompt character class?**
  Some VMS utility prompts are compound, e.g. `ANALYZE/SYSTEM>` or
  `SET HOST 0>`. The class intentionally stays conservative
  (`[A-Za-z0-9_ /]`) rather than `.*`, to avoid matching arbitrary program
  output that happens to end a line with `word>` - still not bulletproof
  against pathological output, but the same class of heuristic risk the
  existing `(?m)^\$ ` alternative already accepts today.
* This changes only `TerminalDefaults.PROMPT_PATTERN`, so `telnet_client_tool`
  and `serial_client_tool` both get the fix (`serial_send_break`'s
  `>>> `-only override is intentionally left alone - it's specifically
  waiting for VAX console firmware after a BREAK, nothing else is valid
  there).

## 4. Known limitations / accepted risk

* Any prompt-detection-by-regex approach can false-positive on ordinary
  program output that happens to look like a prompt (e.g. a report line
  ending in `TOTALS>` or displaying a literal `$` price at end of line).
  This has always been true of the existing default too; the new default
  doesn't meaningfully change the risk profile, just widens legitimate
  coverage.
* The subsystem-prompt alternative cannot distinguish "the real DCL `$`
  prompt" from "we're still inside a nested utility" - a caller batching
  commands across a `RUN`/subsystem boundary (e.g. `RUN TCPIP` then
  several `TCPIP>` commands then `EXIT` back to `$`) still needs a
  precise, situation-specific `prompt_pattern` override for the inner
  commands if they want to detect completion of *each* inner command
  distinctly, rather than only "some prompt, of some kind, showed up."
  The new default is a better generic fallback, not a replacement for
  callers who know exactly which prompt they're waiting for.

## 5. Implementation steps (deferred - do later)

1. Update `TerminalDefaults.PROMPT_PATTERN` in `chuk_mcp_telnet_client/tools.py`
   to the regex in §3.
2. Add regression tests (new cases in `tests/`) that feed synthetic
   buffers through the *default* pattern (not an explicit override, which
   is all the existing tests exercise today - see
   `tests/test_standalone_telnet.py`/`test_standalone_serial.py`, which
   all pass `prompt_pattern=r"(?m)^\$ "` explicitly) and assert it matches:
   * `"VAX96::USER1$ "` (customized DCL prompt)
   * `"$ "` (stock DCL prompt)
   * `"TCPIP> "` and `"TCPIP>"` (with/without trailing space)
   * `"MCL> "`
   * `"SET HOST 0> "` (compound/multi-word facility name)
   * a multi-line buffer where the prompt is NOT the first line (proves
     the `(?m:...)` scoping still finds it mid-buffer)
   * a non-prompt line ending in a bare `$` mid-buffer, NOT at the true
     end of the accumulated output, does **not** falsely complete the
     command (proves the un-scoped `\$ ?$` alternative only fires at the
     real end)
3. Re-run the full existing test suite (`tests/`, `test_long_running.py`)
   to confirm nothing that relied on the old default's narrower matching
   regresses.
4. Update `README.md`'s tool table / examples to mention the new default
   explicitly (it currently documents `prompt_pattern` as a parameter but
   never states what it defaults to or that VMS-style prompts are
   supported out of the box).
5. Bump the version. Note: this project currently repeats
   `"0.6.0"` as a literal default in **three** places -
   `pyproject.toml`, `chuk_mcp_telnet_client/__init__.py`
   (`__version__`), and as a hardcoded field default on *every* response
   dataclass in `models.py` (`server_version: str = "0.6.0"`, at least 7
   occurrences) - all of which need to move together. That repetition is
   itself a minor footgun worth a follow-up single-source-of-truth fix
   (e.g. every dataclass reading `__version__` instead of a literal), but
   is out of scope for this change beyond remembering to grep for all of
   them.
6. Re-verify live against VAX96 using the same `vms-savage` login sequence
   that originally exposed the bug (`USER1`/`user1pass`, `SET DEFAULT
   [.SAVAGE]`, `SET TERMINAL /ESCAPE`, an `MMS` build) via
   `telnet_client_tool` **without** any explicit `prompt_pattern`
   override, confirming all queued commands now execute in one call.
7. Once fixed, `vms-savage`'s own test scripts
   (`scripts/test_idle_quiescence.py`, `test_concurrency_pause.py`, etc.,
   which currently pass their own `prompt_pattern` via
   `PROMPT_PATTERN` constants matching `VAX96::` literally) can optionally
   drop those explicit overrides in favor of the new default - but that's
   the `vms-savage` project's call, not required by this change, and
   those scripts' explicit patterns are arguably still better practice
   (self-documenting, no reliance on a shared library default that could
   change again later).

## 6. Where this was noticed

`vms-savage` project sessions, repeatedly, going back to early telnet
automation work; most recently and concretely during Phase 0.3.2 Stage 2
bugfix verification (2026-09-11/12), where every fresh `telnet_client`
call needed manual `telnet_send_input` follow-ups to work around exactly
this gap.
