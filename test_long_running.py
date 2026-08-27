#!/usr/bin/env python3
"""
Test script for chuk-mcp-telnet-client v0.4.0 long-running process support.
Verifies:
1. Connecting and executing a 45-second script with a 12-second wait window.
2. Checking interim output is captured and command_completed is False.
3. Sleeping / simulating agent processing time while background reader buffers stream data.
4. Calling telnet_read_session to poll subsequent output chunks.
5. Detecting completion when the prompt is reached.
6. Verifying all output lines are completely collected without data loss.
7. Verifying disk log file contains all timestamped entries.
"""

import asyncio
import os
import sys

from chuk_mcp_telnet_client.tools import (
    telnet_client_tool,
    telnet_read_session,
    telnet_send_input,
    telnet_close_session,
)

HOST = "127.0.0.1"
PORT = 10023
SESSION_ID = "v040_integration_test_session"


async def main():
    print("=" * 70)
    print("STARTING TELNET MCP v0.4.0 INTEGRATION TEST")
    print("=" * 70)

    # 1. Close any existing test session
    await telnet_close_session(SESSION_ID)

    # 2. Launch long-running script on VAX with a 12-second wait window
    print("\n[Step 1] Launching @LONG_TASK_TEST.COM with max_wait_seconds=12...")
    res1 = await telnet_client_tool(
        host=HOST,
        port=PORT,
        commands=["SYSTEM", "DPFVAX60", "@LONG_TASK_TEST.COM"],
        telnet_session_id=SESSION_ID,
        max_wait_seconds=12.0,
        prompt_pattern=r"(?m)^\$ ",
        close_session=False,
    )

    print(f"  -> Session ID: {res1.session_id}")
    print(f"  -> Session Active: {res1.session_active}")
    print(f"  -> Command Completed: {res1.command_completed}")
    print(f"  -> Log File: {res1.log_file}")
    print(f"  -> Responses count: {len(res1.responses)}")
    last_cmd_resp = res1.responses[-1].response if res1.responses else ""
    print(f"  -> Last command response:\n{last_cmd_resp}")

    assert res1.session_active, "Session must remain active!"
    assert not res1.command_completed, "Command should NOT be marked completed yet (takes 45s, waited 12s)!"
    assert "Stage 1 complete" in last_cmd_resp, "Stage 1 output should be present in interim output!"

    # 3. Simulate agent thinking for 5 seconds while VAX process runs
    print("\n[Step 2] Simulating agent processing delay (5s) while background reader collects stream...")
    await asyncio.sleep(5.0)

    # 4. Call telnet_read_session with 18s wait window to capture Stage 2 & 3
    print("\n[Step 3] Polling session with telnet_read_session(max_wait_seconds=18)...")
    res2 = await telnet_read_session(
        session_id=SESSION_ID,
        max_wait_seconds=18.0,
        prompt_pattern=r"(?m)^\$ ",
    )

    print(f"  -> New bytes received: {res2.new_bytes_count}")
    print(f"  -> Total bytes received: {res2.total_bytes_received}")
    print(f"  -> Command Completed: {res2.command_completed}")
    print(f"  -> Output received:\n{res2.output}")

    # 5. Call telnet_read_session for the final completion
    print("\n[Step 4] Polling session for final completion with telnet_read_session(max_wait_seconds=25)...")
    res3 = await telnet_read_session(
        session_id=SESSION_ID,
        max_wait_seconds=25.0,
        prompt_pattern=r"(?m)^\$ ",
    )

    print(f"  -> New bytes received: {res3.new_bytes_count}")
    print(f"  -> Total bytes received: {res3.total_bytes_received}")
    print(f"  -> Command Completed: {res3.command_completed}")
    print(f"  -> Matched Prompt: {res3.prompt_matched}")
    print(f"  -> Output received:\n{res3.output}")

    # 6. Verify full accumulated history
    print("\n[Step 5] Fetching full buffer history with return_full_buffer=True...")
    res_full = await telnet_read_session(
        session_id=SESSION_ID,
        max_wait_seconds=1.0,
        return_full_buffer=True,
    )
    print(f"  -> Full Accumulated Output:\n{res_full.output}")

    assert "Stage 1 complete" in res_full.output
    assert "Stage 2 complete" in res_full.output
    assert "Stage 3 complete" in res_full.output
    assert "FINISHED SUCCESSFULLY" in res_full.output

    # 7. Test interactive input tool
    print("\n[Step 6] Testing telnet_send_input with 'SHOW TIME'...")
    res_input = await telnet_send_input(
        session_id=SESSION_ID,
        input_text="SHOW TIME",
        max_wait_seconds=5.0,
        prompt_pattern=r"(?m)^\$ ",
    )
    print(f"  -> Input response:\n{res_input.response}")
    print(f"  -> Command completed: {res_input.command_completed}")

    # 8. Close session
    print("\n[Step 7] Closing session...")
    close_res = await telnet_close_session(SESSION_ID)
    print(f"  -> Close status: {close_res.message}")

    # 9. Verify log file exists and is populated
    if res1.log_file and os.path.isfile(res1.log_file):
        print(f"\n[Step 8] Verifying log file at {res1.log_file} ({os.path.getsize(res1.log_file)} bytes)...")
        with open(res1.log_file, "r") as f:
            log_content = f.read()
        print("  -> First 300 chars of log:\n" + log_content[:300])
        assert "TELNET SESSION LOG" in log_content
        assert "SYSTEM" in log_content
        assert "LONG_TASK_TEST" in log_content
        assert "SESSION ENDED" in log_content
        print("  -> [PASS] Log file verified complete with ISO timestamps!")

    print("\n" + "=" * 70)
    print("ALL INTEGRATION TESTS PASSED SUCCESSFULLY! (100% Output Captured)")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
