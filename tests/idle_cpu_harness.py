"""Measure the idle CPU of a server process that is waiting on a quiet session (v0.9.2).

run_case() starts tests/idle_cpu_child.py as a separate process against a fake telnet
server or a pty (serial) peer, lets the peer send `prelude` bytes of earlier output that
stay pending, then reads the child's CPU time from /proc/<pid>/stat over `window` seconds
of silence. Run it as a script to compare package roots:

    python tests/idle_cpu_harness.py PKG_ROOT [PKG_ROOT ...]
"""

import os
import pty
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
CHILD = os.path.join(HERE, "idle_cpu_child.py")
TICKS = os.sysconf("SC_CLK_TCK")


def prelude_bytes(n):
    """About n bytes of progress lines, none of which looks like a prompt."""
    line = b"PROGRESS stage %05d of the long run: still working, nothing to report yet\r\n"
    out = bytearray()
    i = 0
    while len(out) < n:
        out += line % i
        i += 1
    return bytes(out)


def cpu_seconds(pid):
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / TICKS  # utime + stime


def _sample(proc, window):
    t0, c0 = time.monotonic(), cpu_seconds(proc.pid)
    time.sleep(window)
    t1, c1 = time.monotonic(), cpu_seconds(proc.pid)
    cpu = c1 - c0
    return {"cpu_seconds": cpu, "wall_seconds": t1 - t0, "percent_of_core": 100.0 * cpu / (t1 - t0)}


def run_case(kind, prelude, window=30.0, pkg_root=None, settle=2.0, trickle=None):
    """`trickle`: seconds between single bytes the peer sends from the start of the settle
    until the end of the window (output that never makes a prompt). Returns {"cpu_seconds", "wall_seconds", "percent_of_core", "load1", "module"}."""
    env = dict(os.environ)
    if pkg_root:
        env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    log_dir = tempfile.mkdtemp(prefix="idlecpu_", dir=os.environ.get("TMPDIR") or None)
    keep = []
    if kind == "telnet":
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        target = str(srv.getsockname()[1])
        keep.append(srv)
    else:
        master, slave = pty.openpty()
        tty.setraw(master)
        tty.setraw(slave)
        target = os.ttyname(slave)
        keep += [master, slave]
    stop = threading.Event()
    proc = subprocess.Popen(
        [sys.executable, CHILD, kind, target, log_dir, str(window + settle + 20)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, cwd=log_dir)
    try:
        conn = None
        if kind == "telnet":
            keep[0].settimeout(20)
            conn, _ = keep[0].accept()

        def send_prelude():
            if not prelude:
                return
            try:
                if kind == "telnet":
                    conn.sendall(prelude)
                else:
                    view = memoryview(prelude)
                    while view:
                        view = view[os.write(master, view[:4096]):]
            except OSError:
                pass

        assert proc.stdout.readline().strip() == "OPEN"
        threading.Thread(target=send_prelude, daemon=True).start()
        assert proc.stdout.readline().strip() == "WAITING"
        def send_trickle():
            while not stop.wait(trickle):
                try:
                    if kind == "telnet":
                        conn.sendall(b".")
                    else:
                        os.write(master, b".")
                except OSError:
                    return

        if trickle:
            threading.Thread(target=send_trickle, daemon=True).start()
        time.sleep(settle)  # the first look at the prelude is a one-off cost, not idle cost
        result = _sample(proc, window)
        result["load1"] = os.getloadavg()[0]
        result["module"] = subprocess.run(
            [sys.executable, "-c", "import chuk_mcp_telnet_client as m;print(m.__file__, m.__version__)"],
            env=env, capture_output=True, text=True, cwd=log_dir).stdout.strip()
        return result
    finally:
        stop.set()
        proc.kill()
        proc.wait()
        for obj in keep:
            try:
                obj.close() if hasattr(obj, "close") else os.close(obj)
            except OSError:
                pass


if __name__ == "__main__":
    window = float(os.environ.get("IDLE_WINDOW", "30"))
    for root in sys.argv[1:] or [None]:
        for kind in ("telnet", "serial"):
            for size in (0, 300_000):
                r = run_case(kind, prelude_bytes(size), window=window, pkg_root=root)
                print(f"{kind:6} prelude={size:>6} {r['percent_of_core']:6.2f}% of a core "
                      f"({r['cpu_seconds']:.3f} s CPU in {r['wall_seconds']:.1f} s) "
                      f"load1={r['load1']:.2f} {r['module']}", flush=True)
