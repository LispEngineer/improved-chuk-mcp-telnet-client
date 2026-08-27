"""
Telnet session logging utilities.
Provides permanent disk logging with ISO-8601 timestamps for all telnet interactions.
"""

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class TelnetSessionLogger:
    """Manages persistent disk logging for telnet sessions."""

    def __init__(self, log_dir: Optional[str] = None):
        if not log_dir:
            log_dir = os.environ.get("TELNET_LOG_DIR")

        # Smart fallback: if in a workspace with a logs/ folder, use that; otherwise ~/.mcp-telnet-logs
        if not log_dir:
            cwd_logs = os.path.join(os.getcwd(), "logs")
            if os.path.isdir(cwd_logs):
                log_dir = cwd_logs
            else:
                log_dir = os.path.expanduser("~/.mcp-telnet-logs")
        else:
            log_dir = os.path.expanduser(log_dir)

        self.log_dir = log_dir
        if self.log_dir:
            try:
                os.makedirs(self.log_dir, exist_ok=True)
            except Exception as e:
                logger.warning(f"Could not create telnet log directory '{self.log_dir}': {e}")
                self.log_dir = None

    def get_session_log_path(self, session_id: str, host: str, port: int) -> Optional[str]:
        """Generate the full path to the session log file."""
        if not self.log_dir:
            return None
        # Clean session ID for safe filename
        clean_id = session_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        return os.path.join(self.log_dir, f"{clean_id}.log")

    def log_session_start(
        self, session_id: str, host: str, port: int, banner: str
    ) -> Optional[str]:
        """Log the initial connection and banner."""
        log_path = self.get_session_log_path(session_id, host, port)
        if not log_path:
            return None

        now_str = datetime.now().astimezone().isoformat()
        header = (
            f"{'='*80}\n"
            f"TELNET SESSION LOG: {host}:{port}\n"
            f"Session ID : {session_id}\n"
            f"Started At : {now_str}\n"
            f"{'='*80}\n\n"
        )
        try:
            if not os.path.exists(log_path):
                with open(log_path, "w", encoding="utf-8", errors="replace") as f:
                    f.write(header)
                    if banner:
                        f.write(f"[{now_str}] [INITIAL BANNER]\n{banner}\n\n")
            elif banner:
                with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(f"[{now_str}] [INITIAL BANNER]\n{banner}\n\n")
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing to telnet log file '{log_path}': {e}")
            return None

    def log_command(
        self, session_id: str, host: str, port: int, command: str, response: str
    ) -> Optional[str]:
        """Log a command and its initial response."""
        log_path = self.get_session_log_path(session_id, host, port)
        if not log_path:
            return None

        now_str = datetime.now().astimezone().isoformat()
        entry = (
            f"[{now_str}] >>> {command}\n"
            f"{response}\n"
            f"{'-'*40}\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(entry)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing to telnet log file '{log_path}': {e}")
            return None

    def log_stream_chunk(
        self, session_id: str, host: str, port: int, chunk: str
    ) -> Optional[str]:
        """Log a stream chunk received asynchronously in background."""
        log_path = self.get_session_log_path(session_id, host, port)
        if not log_path or not chunk:
            return None

        now_str = datetime.now().astimezone().isoformat()
        entry = (
            f"[{now_str}] [STREAM DATA]\n"
            f"{chunk}\n"
            f"{'-'*40}\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(entry)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing stream chunk to log '{log_path}': {e}")
            return None

    def log_input(
        self, session_id: str, host: str, port: int, input_text: str
    ) -> Optional[str]:
        """Log interactive input sent to session."""
        log_path = self.get_session_log_path(session_id, host, port)
        if not log_path:
            return None

        now_str = datetime.now().astimezone().isoformat()
        entry = (
            f"[{now_str}] >>> [INPUT] {input_text}\n"
            f"{'-'*40}\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(entry)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing input to log '{log_path}': {e}")
            return None

    def log_session_end(
        self, session_id: str, host: str, port: int, reason: str = "Closed"
    ) -> Optional[str]:
        """Log the session termination."""
        log_path = self.get_session_log_path(session_id, host, port)
        if not log_path:
            return None

        now_str = datetime.now().astimezone().isoformat()
        footer = (
            f"\n[{now_str}] SESSION ENDED ({reason})\n"
            f"{'='*80}\n\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(footer)
            return log_path
        except Exception as e:
            logger.warning(f"Failed writing to telnet log file '{log_path}': {e}")
            return None
