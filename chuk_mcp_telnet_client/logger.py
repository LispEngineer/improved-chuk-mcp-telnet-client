"""
Session logging utilities for Telnet and Serial connections.
Provides clean terminal transcript logging and optional timestamped debug logging.
Guarantees a single persistent log file per session without creating incremental suffix files.
"""

import logging
import os
import threading
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# The text of the record written in place of a send that follows a password prompt.
INPUT_SUPPRESSED_TEXT = "(suppressed: password prompt)"


class SessionLogger:
    """Manages persistent disk logging for Telnet and Serial sessions."""

    def __init__(self, log_dir: Optional[str] = None):
        if not log_dir:
            log_dir = os.environ.get("TELNET_LOG_DIR") or os.environ.get("SERIAL_LOG_DIR")

        # Smart fallback: if in a workspace with a logs/ folder, use that; otherwise ~/.mcp-terminal-logs
        if not log_dir:
            cwd_logs = os.path.join(os.getcwd(), "logs")
            if os.path.isdir(cwd_logs):
                log_dir = cwd_logs
            else:
                log_dir = os.path.expanduser("~/.mcp-terminal-logs")
        else:
            log_dir = os.path.expanduser(log_dir)

        self.log_dir = log_dir
        if self.log_dir:
            try:
                os.makedirs(self.log_dir, exist_ok=True)
            except Exception as e:
                logger.warning(f"Could not create log directory '{self.log_dir}': {e}")
                self.log_dir = None

        # Tracks assigned log file paths per session_id so reconnects/appends use the same file
        self._session_files: Dict[str, str] = {}
        # Tracks timestamp mode per session
        self._timestamp_mode: Dict[str, bool] = {}
        # The session reader threads and the tool calls append to the same transcript
        # from different threads (v0.7.1); one entry is written at a time.
        self._write_lock = threading.Lock()

    def get_session_log_path(self, session_id: str) -> Optional[str]:
        """
        Generate or retrieve the full path to the session log file.
        Always maps session_id to <log_dir>/<clean_id>.log to guarantee a single
        log file per session without creating incremental suffix files.
        """
        if not self.log_dir:
            return None

        if session_id in self._session_files:
            return self._session_files[session_id]

        clean_id = session_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        candidate = os.path.join(self.log_dir, f"{clean_id}.log")
        self._session_files[session_id] = candidate
        return candidate

    def log_session_start(
        self,
        session_id: str,
        target: str,
        protocol: str = "TELNET",
        banner: str = "",
        timestamp_chunks: bool = False,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> Optional[str]:
        """Log the initial connection and banner."""
        if host and port and not target:
            target = f"{host}:{port}"

        self._timestamp_mode[session_id] = timestamp_chunks
        log_path = self.get_session_log_path(session_id)
        if not log_path:
            return None

        now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        header = (
            f"{'='*80}\n"
            f"{protocol.upper()} SESSION LOG: {target}\n"
            f"Server Version : chuk-mcp-terminal-client v0.9.1\n"
            f"Session ID     : {session_id}\n"
            f"Started At     : {now_str}\n"
            f"{'='*80}\n\n"
        )
        try:
            mode = "a" if os.path.exists(log_path) else "w"
            with self._write_lock, open(log_path, mode, encoding="utf-8", errors="replace") as f:
                if mode == "a":
                    f.write(f"\n{header}")
                else:
                    f.write(header)
                if banner:
                    if timestamp_chunks:
                        f.write(f"[{datetime.now().astimezone().isoformat()}] [INITIAL BANNER]\n{banner}\n\n")
                    else:
                        f.write(f"{banner}\n")
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing to log file '{log_path}': {e}")
            return None

    def log_command(
        self,
        session_id: str,
        command: str,
        response: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> Optional[str]:
        """Inputs may contain credentials; output is logged only at ingestion."""
        return self.get_session_log_path(session_id)

    def log_stream_chunk(
        self,
        session_id: str,
        chunk: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        at: Optional[float] = None,
        raw: bytes = b"",
    ) -> Optional[str]:
        """Log a stream chunk received asynchronously in background.

        `at` (a time.time() value) is when the chunk arrived; the stamp uses it when given.
        """
        log_path = self.get_session_log_path(session_id)
        if not log_path or (not chunk and not raw):
            return None

        timestamp_chunks = self._timestamp_mode.get(session_id, False)
        try:
            with self._write_lock, open(log_path, "a", encoding="utf-8", errors="replace") as f:
                if timestamp_chunks:
                    when = datetime.fromtimestamp(at) if at is not None else datetime.now()
                    now_str = when.astimezone().isoformat()
                    entry = f"[{now_str}] [STREAM DATA]\n{chunk}\n{'-'*40}\n"
                else:
                    # Clean contiguous stream output
                    entry = chunk
                f.write(entry)
                if raw:
                    with open(log_path + ".raw", "ab") as raw_file:
                        raw_file.write(raw)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing stream chunk to log '{log_path}': {e}")
            return None

    def log_input(
        self,
        session_id: str,
        input_text: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> Optional[str]:
        """Never persist input, which may be a password or secret command.

        (Kept as a no-op. The opt-in input log of v0.9.1 is log_input_record, which
        the tools call only for a session opened or switched with log_input=True.)
        """
        return self.get_session_log_path(session_id)

    def enable_timestamps(self, session_id: str) -> None:
        """Stamp the session's received-output records (as timestamp_chunks=True does)."""
        self._timestamp_mode[session_id] = True

    def log_input_record(
        self,
        session_id: str,
        text: str,
        at: Optional[float] = None,
        suppressed: bool = False,
    ) -> Optional[str]:
        """Write one `[INPUT]` record: what a tool call sent, stamped to the millisecond.

        `at` is a time.time() value, the same clock that stamps received-output
        records (log_stream_chunk's `at`), so the gap from an [INPUT] record to the
        next received record is the host's reply time. With suppressed=True the text
        is not written, only a note that it was withheld (a send after a password
        prompt). Nothing is written to the `.log.raw` sidecar, which holds received
        bytes only.
        """
        log_path = self.get_session_log_path(session_id)
        if not log_path:
            return None

        when = datetime.fromtimestamp(at) if at is not None else datetime.now()
        stamp = when.astimezone().isoformat(timespec="milliseconds")
        body = INPUT_SUPPRESSED_TEXT if suppressed else text
        entry = f"[{stamp}] [INPUT] {body}\n"
        try:
            with self._write_lock, open(log_path, "a+b") as f:
                # An unterminated stream chunk must not run into the record.
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        entry = "\n" + entry
                f.write(entry.encode("utf-8", errors="replace"))
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing input record to log '{log_path}': {e}")
            return None

    def log_config_change(
        self,
        session_id: str,
        change_description: str,
    ) -> Optional[str]:
        """Log a hardware or speed configuration change event."""
        log_path = self.get_session_log_path(session_id)
        if not log_path:
            return None

        now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n[CONFIG CHANGE ({now_str}): {change_description}]\n"
        try:
            with self._write_lock, open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(entry)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing config change to log '{log_path}': {e}")
            return None

    def log_session_end(
        self,
        session_id: str,
        reason: str = "Closed",
        total_bytes: int = 0,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> Optional[str]:
        """Log session termination."""
        log_path = self.get_session_log_path(session_id)
        if not log_path:
            return None

        now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        footer = (
            f"\n{'='*80}\n"
            f"SESSION ENDED ({reason}) at {now_str}\n"
            f"Total Bytes Transferred: {total_bytes}\n"
            f"{'='*80}\n\n"
        )
        try:
            with self._write_lock, open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(footer)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing session end to log '{log_path}': {e}")
            return None


# Backward compatibility alias
TelnetSessionLogger = SessionLogger
