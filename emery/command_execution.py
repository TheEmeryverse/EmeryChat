"""Bounded, opt-in shell command execution for Emery.

This is intentionally a small first slice of terminal support.  It keeps the
execution boundary in one module so a later approval queue or sandbox backend
can replace the local runner without changing the model-facing tool schema.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

from emery.config import (
    _to_bool,
    _to_float,
    _to_int,
    BASE_DIR,
    COMMAND_EXECUTION_APPROVAL_TIMEOUT_SECONDS,
    COMMAND_EXECUTION_BACKEND,
    COMMAND_EXECUTION_DOCKER_BINARY,
    COMMAND_EXECUTION_DOCKER_IMAGE,
    COMMAND_EXECUTION_SSH_BINARY,
    COMMAND_EXECUTION_SSH_HOST,
    COMMAND_EXECUTION_SSH_USER,
    COMMAND_EXECUTION_SSH_KEY,
    COMMAND_EXECUTION_SSH_PORT,
)


COMMAND_EXECUTION_ENABLED = _to_bool(os.getenv("ENABLE_COMMAND_EXECUTION"), False)
COMMAND_EXECUTION_CWD = os.getenv("COMMAND_EXECUTION_CWD", str(BASE_DIR)).strip() or str(BASE_DIR)
COMMAND_EXECUTION_TIMEOUT_SECONDS = max(
    1.0, _to_float(os.getenv("COMMAND_EXECUTION_TIMEOUT_SECONDS"), 30.0)
)
COMMAND_EXECUTION_MAX_TIMEOUT_SECONDS = max(
    COMMAND_EXECUTION_TIMEOUT_SECONDS,
    _to_float(os.getenv("COMMAND_EXECUTION_MAX_TIMEOUT_SECONDS"), 120.0),
)
COMMAND_EXECUTION_MAX_OUTPUT_CHARS = max(
    1_000, _to_int(os.getenv("COMMAND_EXECUTION_MAX_OUTPUT_CHARS"), 12_000)
)
COMMAND_EXECUTION_ALLOW_DANGEROUS = _to_bool(
    os.getenv("COMMAND_EXECUTION_ALLOW_DANGEROUS"), False
)
COMMAND_EXECUTION_SHELL = os.getenv("COMMAND_EXECUTION_SHELL", "/bin/sh").strip() or "/bin/sh"


_DANGEROUS_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(?:-[^\s]*r[^\s]*|--recursive)(?:\s|$)", re.IGNORECASE), "recursive delete"),
    (re.compile(r"\b(?:mkfs|fdisk|parted)\b", re.IGNORECASE), "disk formatting or partitioning"),
    (re.compile(r"\bdd\b[^\n]*\bof=", re.IGNORECASE), "raw disk/device write"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff|halt)\b", re.IGNORECASE), "system power control"),
    (re.compile(r"\bsystemctl\s+(?:stop|restart|disable|mask)\b", re.IGNORECASE), "service control"),
    (re.compile(r"\b(?:sudo|su)\b", re.IGNORECASE), "privilege escalation"),
    (re.compile(r"\b(?:kill|pkill|killall)\b", re.IGNORECASE), "process termination"),
    (re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sh|bash|zsh)\b", re.IGNORECASE), "downloaded script execution"),
    (re.compile(r"\b(?:DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE)\b", re.IGNORECASE), "destructive SQL"),
    (re.compile(r"\bDELETE\s+FROM\b(?![^;\n]*\bWHERE\b)", re.IGNORECASE), "unfiltered SQL delete"),
    (re.compile(r"(?:>\s*|>>\s*)/(?:etc|dev|boot|proc|sys)(?:/|\s|$)", re.IGNORECASE), "system path overwrite"),
    (re.compile(r":\(\)\s*\{.*:\|:.*\};\s*:", re.IGNORECASE), "fork bomb"),
    (re.compile(r"\bgit\s+push\b", re.IGNORECASE), "remote repository write"),
)

_SECRET_OUTPUT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|password|secret|cookie)\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
)


def _dangerous_reason(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def _redact_output(output: str) -> str:
    redacted = output
    for pattern, replacement in _SECRET_OUTPUT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _resolve_working_directory(working_directory: str | None) -> Path:
    raw_path = str(working_directory or COMMAND_EXECUTION_CWD).strip()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(COMMAND_EXECUTION_CWD).expanduser() / path
    return path.resolve()


def _command_environment() -> dict[str, str]:
    """Pass useful runtime settings while avoiding obvious application secrets."""
    safe_names = {
        "HOME", "LANG", "LC_ALL", "PATH", "PWD", "SHELL", "TERM", "TMPDIR",
        "USER", "VIRTUAL_ENV", "CONDA_PREFIX", "UV_PROJECT_ENVIRONMENT",
    }
    blocked_fragments = (
        "API_KEY", "AUTH", "COOKIE", "CREDENTIAL", "KEY", "PASSWORD", "PRIVATE_KEY",
        "SECRET", "TOKEN",
    )
    return {
        key: value
        for key, value in os.environ.items()
        if key in safe_names or not any(fragment in key.upper() for fragment in blocked_fragments)
    }


def _run_command_sync(command: str, cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    process = None
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            executable=COMMAND_EXECUTION_SHELL,
            cwd=str(cwd),
            env=_command_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        output, _ = process.communicate(timeout=timeout_seconds)
        return {
            "status": "completed",
            "exit_code": process.returncode,
            "output": _redact_output(output or ""),
        }
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            output, _ = process.communicate()
        else:
            output = exc.output or ""
        return {
            "status": "timeout",
            "exit_code": None,
            "output": _redact_output(output or ""),
            "error": f"Command exceeded the {timeout_seconds:g}-second timeout and was terminated.",
        }
    except OSError as exc:
        return {
            "status": "error",
            "exit_code": None,
            "output": "",
            "error": f"Unable to execute command: {exc}",
        }


async def run_command(
    command: str,
    working_directory: str | None = None,
    timeout_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Run one bounded, non-interactive shell command on the Emery host."""
    if not COMMAND_EXECUTION_ENABLED:
        return {
            "status": "disabled",
            "exit_code": None,
            "output": "",
            "error": "Command execution is disabled. Set ENABLE_COMMAND_EXECUTION=true to enable it.",
        }

    if not isinstance(command, str) or not command.strip():
        return {"status": "error", "exit_code": None, "output": "", "error": "command must be a non-empty string"}
    command = command.strip()
    if len(command) > 4_000:
        return {"status": "error", "exit_code": None, "output": "", "error": "command is limited to 4000 characters"}

    if timeout_seconds is None:
        timeout = COMMAND_EXECUTION_TIMEOUT_SECONDS
    elif isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        return {"status": "error", "exit_code": None, "output": "", "error": "timeout_seconds must be a number"}
    else:
        timeout = float(timeout_seconds)
    if timeout <= 0 or timeout > COMMAND_EXECUTION_MAX_TIMEOUT_SECONDS:
        return {
            "status": "error",
            "exit_code": None,
            "output": "",
            "error": f"timeout_seconds must be between 1 and {COMMAND_EXECUTION_MAX_TIMEOUT_SECONDS:g}",
        }

    reason = _dangerous_reason(command)
    if reason and not COMMAND_EXECUTION_ALLOW_DANGEROUS:
        from emery import globals
        from emery.command_approval import request_command_approval

        approval = await request_command_approval(
            command,
            reason,
            chat_id=globals.TARGET_CHAT_ID.get(),
            user_id=globals.current_user_id.get(),
            thread_id=globals.CURRENT_THREAD_ID.get(),
            timeout_seconds=COMMAND_EXECUTION_APPROVAL_TIMEOUT_SECONDS,
        )
        if not approval.get("approved"):
            return {
                "status": "blocked",
                "exit_code": None,
                "output": "",
                "error": (
                    f"Command was not executed: {approval.get('message', 'approval denied')} "
                    f"(safety policy: {reason})"
                ),
            }

    if COMMAND_EXECUTION_BACKEND != "local":
        try:
            from emery.command_backends import CommandBackendConfig, create_command_backend

            backend = create_command_backend(
                COMMAND_EXECUTION_BACKEND,
                config=CommandBackendConfig(
                    backend=COMMAND_EXECUTION_BACKEND,
                    # Remote/container paths are not assumed to match the
                    # Emery host's local project directory.
                    cwd=working_directory,
                    timeout_seconds=timeout,
                    max_timeout_seconds=COMMAND_EXECUTION_MAX_TIMEOUT_SECONDS,
                    max_output_chars=COMMAND_EXECUTION_MAX_OUTPUT_CHARS,
                    docker_binary=COMMAND_EXECUTION_DOCKER_BINARY,
                    docker_image=COMMAND_EXECUTION_DOCKER_IMAGE,
                    ssh_binary=COMMAND_EXECUTION_SSH_BINARY,
                    ssh_host=COMMAND_EXECUTION_SSH_HOST,
                    ssh_user=COMMAND_EXECUTION_SSH_USER,
                    ssh_port=COMMAND_EXECUTION_SSH_PORT,
                    ssh_key=COMMAND_EXECUTION_SSH_KEY,
                ),
            )
            result = await backend.execute(command, cwd=working_directory, timeout_seconds=timeout)
        except ValueError as exc:
            return {"status": "error", "exit_code": None, "output": "", "error": str(exc)}
        result.setdefault("command", command)
        if working_directory is not None:
            result.setdefault("working_directory", str(working_directory))
        return result

    try:
        cwd = _resolve_working_directory(working_directory)
    except (OSError, RuntimeError) as exc:
        return {"status": "error", "exit_code": None, "output": "", "error": f"Invalid working directory: {exc}"}
    if not cwd.is_dir():
        return {"status": "error", "exit_code": None, "output": "", "error": f"Working directory is not a directory: {cwd}"}

    result = await asyncio.to_thread(_run_command_sync, command, cwd, timeout)
    output = str(result.get("output") or "")
    if not output and result.get("stdout"):
        output = str(result.get("stdout") or "")
    if result.get("stderr"):
        output = f"{output}\n{result['stderr']}" if output else str(result["stderr"])
    result["output_truncated"] = len(output) > COMMAND_EXECUTION_MAX_OUTPUT_CHARS
    if result["output_truncated"]:
        result["output"] = output[:COMMAND_EXECUTION_MAX_OUTPUT_CHARS] + "\n[output truncated]"
    result["command"] = command
    result["working_directory"] = str(cwd)
    return result
