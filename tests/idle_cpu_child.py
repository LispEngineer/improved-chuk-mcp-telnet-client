"""The process whose idle CPU tests/idle_cpu_harness.py measures (v0.9.2).

Usage: python idle_cpu_child.py telnet|serial TARGET LOG_DIR WAIT_SECONDS

Opens a session on the fake peer at TARGET (a TCP port, or a pty path), prints OPEN,
gives the peer 3 s to send its earlier output (which stays pending, unconsumed), prints
WAITING, then waits WAIT_SECONDS for a prompt that never comes - the shape of a long run
that prints little - and prints DONE. Imports whatever chuk_mcp_telnet_client is first on
PYTHONPATH, so the same script measures 0.9.1 and 0.9.2.
"""

import asyncio
import sys

import chuk_mcp_telnet_client.tools as tools

PATTERN = r"(?:[A-Za-z0-9]+::)?USER1\$ $"


async def main(kind, target, log_dir, wait):
    if kind == "telnet":
        await tools.telnet_client_tool(
            host="127.0.0.1", port=int(target), commands=[], telnet_session_id="idle",
            log_dir=log_dir, response_wait=0.3)
        read = tools.telnet_read_session
    else:
        await tools.serial_client_tool(
            port=target, commands=[], serial_session_id="idle", log_dir=log_dir,
            response_wait=0.3)
        read = tools.serial_read_session
    print("OPEN", flush=True)
    await asyncio.sleep(3.0)
    print("WAITING", flush=True)
    res = await read(session_id="idle", max_wait_seconds=wait, prompt_pattern=PATTERN)
    print("DONE", res.command_completed, len(res.output), flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])))
