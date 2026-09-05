"""
Session logging utilities for Telnet and Serial connections.
Provides clean terminal transcript logging and optional timestamped debug logging.
Guarantees that existing log files are never overwritten.
"""

import logging
import os
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


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

    def get_session_log_path(self, session_id: str) -> Optional[str]:
        """
        Generate or retrieve the full path to the session log file.
        Guarantees that existing files are never overwritten:
        If <session_id>.log already exists, sequentially generates <session_id>_1.log, etc.
        """
        if not self.log_dir:
            return None

        # If we already resolved a file for this session in this instance, reuse it
        if session_id in self._session_files:
            return self._session_files[session_id]

        clean_id = session_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        candidate = os.path.join(self.log_dir, f"{clean_id}.log")

        if not os.path.exists(candidate):
            self._session_files[session_id] = candidate
            return candidate

        index = 1
        while True:
            candidate = os.path.join(self.log_dir, f"{clean_id}_{index}.log")
            if not os.path.exists(candidate):
                self._session_files[session_id] = candidate
                return candidate
            index += 1

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
            f"Server Version : chuk-mcp-terminal-client v0.5.0\n"
            f"Session ID     : {session_id}\n"
            f"Started At     : {now_str}\n"
            f"{'='*80}\n\n"
        )
        try:
            # File is guaranteed to be new due to get_session_log_path no-overwrite logic
            with open(log_path, "w", encoding="utf-8", errors="replace") as f:
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
        """Log a command and its response."""
        log_path = self.get_session_log_path(session_id)
        if not log_path:
            return None

        timestamp_chunks = self._timestamp_mode.get(session_id, False)
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                if timestamp_chunks:
                    now_str = datetime.now().astimezone().isoformat()
                    entry = f"[{now_str}] >>> {command}\n{response}\n{'-'*40}\n"
                else:
                    # In clean transcript mode, if command echo wasn't already received in response,
                    # ensure command appears naturally
                    if response.startswith(command):
                        entry = f"{response}\n"
                    else:
                        entry = f"{command}\n{response}\n"
                f.write(entry)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing command to log '{log_path}': {e}")
            return None

    def log_stream_chunk(
        self,
        session_id: str,
        chunk: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> Optional[str]:
        """Log a stream chunk received asynchronously in background."""
        log_path = self.get_session_log_path(session_id)
        if not log_path or not chunk:
            return None

        timestamp_chunks = self._timestamp_mode.get(session_id, False)
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                if timestamp_chunks:
                    now_str = datetime.now().astimezone().isoformat()
                    entry = f"[{now_str}] [STREAM DATA]\n{chunk}\n{'-'*40}\n"
                else:
                    # Clean contiguous stream output
                    entry = chunk
                f.write(entry)
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
        """Log interactive input sent to session."""
        log_path = self.get_session_log_path(session_id)
        if not log_path:
            return None

        timestamp_chunks = self._timestamp_mode.get(session_id, False)
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                if timestamp_chunks:
                    now_str = datetime.now().astimezone().isoformat()
                    entry = f"[{now_str}] >>> [INPUT] {input_text}\n{'-'*40}\n"
                else:
                    entry = f"{input_text}\n"
                f.write(entry)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing input to log '{log_path}': {e}")
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
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
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
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(footer)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing session end to log '{log_path}': {e}")
            return None


# Backward compatibility alias
TelnetSessionLogger = SessionLogger
