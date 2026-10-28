#!/usr/bin/env python3
"""M1 sabotage runner: plant one fault in tools.py, run the tests that must catch it,
restore the file unconditionally and check its hash. Run from the worktree root."""
import hashlib
import shutil
import subprocess
import sys
import time

TOOLS = "chuk_mcp_telnet_client/tools.py"
BACKUP = sys.argv[1]  # a scratch path
OUT = sys.argv[2]

SABOTAGES = [
    ("S1 close does not stop the reader or close the transport",
     "    session.is_active = False\n    session.stop_event.set()\n\n    if isinstance(session, TelnetSession):\n        sock = session.telnet.get_socket()",
     "    session.is_active = False\n    return\n    session.stop_event.set()\n\n    if isinstance(session, TelnetSession):\n        sock = session.telnet.get_socket()",
     "close_while or serial_close"),
    ("S2 close stops the reader but never closes the socket",
     "    if isinstance(session, TelnetSession):\n        try:\n            session.telnet.close()",
     "    if False:\n        try:\n            session.telnet.close()",
     "100_cycles"),
    ("S3 wait ignores settle (returns at the first change)",
     "            quiet = now - session.last_rx_monotonic >= settle\n",
     "            quiet = True\n",
     "returns_after_settle or wait_for_change_pattern"),
    ("S4 wait ignores the last screen read (baseline = screen at call start)",
     "        baseline = session.last_read_sig\n        if baseline is None:",
     "        baseline = None\n        if baseline is None:",
     "sees_change_made_before"),
    ("S5 wait ignores the pattern (ends at any change)",
     "        done = (matched is not None) if compiled is not None else changed\n",
     "        done = changed\n",
     "wait_for_change_pattern"),
    ("S6 the reader thread is never started (0.7.0's fault: nothing reads between calls)",
     "    session.reader_thread = thread\n    thread.start()\n",
     "    session.reader_thread = thread\n",
     "idle_telnet or idle_serial or stamped_on_arrival"),
]


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main():
    out = open(OUT, "w")

    def say(s):
        print(s)
        out.write(s + "\n")
        out.flush()

    say("sabotage run %s, load %s" % (time.strftime("%F %T %Z"), open("/proc/loadavg").read().strip()))
    clean = sha(TOOLS)
    say("tools.py sha256 before: %s" % clean)
    for name, old, new, kexpr in SABOTAGES:
        shutil.copy2(TOOLS, BACKUP)
        try:
            s = open(TOOLS).read()
            assert s.count(old) == 1, "%s: anchor found %d times" % (name, s.count(old))
            open(TOOLS, "w").write(s.replace(old, new))
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/test_m1_live_reader.py", "-p", "no:cacheprovider",
                 "-q", "-s", "-k", kexpr], capture_output=True, text=True, timeout=600)
            say("=== %s  (-k %r): pytest exit %d" % (name, kexpr, r.returncode))
            for ln in r.stdout.splitlines():
                if ln.startswith(("E ", "FAILED", "PASSED")) or " passed" in ln or " failed" in ln \
                        or ln.startswith("tests/") or "load " in ln:
                    say("    " + ln[:220])
        finally:
            shutil.copy2(BACKUP, TOOLS)
        assert sha(TOOLS) == clean, "restore failed after %s" % name
    say("tools.py sha256 after:  %s (restored: %s)" % (sha(TOOLS), sha(TOOLS) == clean))


if __name__ == "__main__":
    main()
