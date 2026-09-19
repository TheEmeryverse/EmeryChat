"""Pluggable command execution backends.

This module deliberately stops at the execution boundary.  Approval policy,
feature flags, and tool registration belong to the caller.  Each backend
offers the same bounded, non-interactive interface and returns a JSON-friendly
result instead of leaking subprocess exceptions to the model.

The Docker backend is one-shot by design for this first slice.  It does not
forward the host environment or mount host paths.  Its ``cwd`` is therefore a
path inside the image.  The SSH backend likewise forwards no environment
variables from Emery.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass, replace
from typing import Any, Sequence

from emery.command_execution import (
    COMMAND_EXECUTION_CWD,
    COMMAND_EXECUTION_MAX_OUTPUT_CHARS,
    _redact_output,
    _resolve_working_directory,
    _run_command_sync,
)


SUPPORTED_BACKENDS = frozenset({"local", "docker", "ssh"})
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_DOCKER_IMAGE = "python:3.11-slim"


@dataclass(frozen=True)
class CommandBackendConfig:
    """Settings shared by all backends plus backend-specific connection data."""

    backend: str = "local"
    cwd: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_timeout_seconds: float = DEFAULT_MAX_TIMEOUT_SECONDS
    max_output_chars: int = COMMAND_EXECUTION_MAX_OUTPUT_CHARS

    docker_binary: str = "docker"
    docker_image: str = DEFAULT_DOCKER_IMAGE

    ssh_binary: str = "ssh"
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_port: int | None = None
    ssh_key: str | None = None


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|password|secret|cookie)\s*[:=]\s*)[^\s,;&]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:api[_-]?key|token|password|secret)=)[^&#\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(://[^\s/@:]+:)[^\s/@]+(@)"), r"\1[REDACTED]\2"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
)


def _redact_text(value: Any) -> str:
    """Redact common credential forms before data reaches a tool result."""

    redacted = _redact_output(str(value or ""))
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _bounded_timeout(value: float | int | None, config: CommandBackendConfig) -> tuple[float | None, str | None]:
    timeout = config.timeout_seconds if value is None else value
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return None, "timeout_seconds must be a number"
    timeout = float(timeout)
    try:
        max_timeout = max(1.0, float(config.max_timeout_seconds))
    except (TypeError, ValueError):
        return None, "max_timeout_seconds must be a number"
    if timeout <= 0 or timeout > max_timeout:
        return None, f"timeout_seconds must be between 1 and {max_timeout:g}"
    return timeout, None


def _validate_command(command: str, config: CommandBackendConfig) -> tuple[str | None, str | None]:
    if not isinstance(command, str) or not command.strip():
        return None, "command must be a non-empty string"
    command = command.strip()
    if len(command) > 4_000:
        return None, "command is limited to 4000 characters"
    try:
        max_output = int(config.max_output_chars)
    except (TypeError, ValueError):
        return None, "max_output_chars must be an integer"
    if max_output < 1_000:
        return None, "max_output_chars must be at least 1000"
    return command, None


def _limit_result_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars] + "\n[output truncated]", True


def _result(
    *,
    backend: str,
    command: str,
    cwd: str | None,
    status: str,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    error: str | None = None,
    max_output_chars: int = COMMAND_EXECUTION_MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    safe_stdout = _redact_text(stdout)
    safe_stderr = _redact_text(stderr)
    combined = safe_stdout
    if safe_stderr:
        combined = f"{combined}\n{safe_stderr}" if combined else safe_stderr
    safe_output, truncated = _limit_result_text(combined, max_output_chars)
    safe_stdout, stdout_truncated = _limit_result_text(safe_stdout, max_output_chars)
    safe_stderr, stderr_truncated = _limit_result_text(safe_stderr, max_output_chars)
    return {
        "status": status,
        "backend": backend,
        "exit_code": exit_code,
        "output": safe_output,
        "stdout": safe_stdout,
        "stderr": safe_stderr,
        "output_truncated": truncated or stdout_truncated or stderr_truncated,
        "error": _redact_text(error) if error else None,
        "command": _redact_text(command),
        "working_directory": _redact_text(cwd) if cwd else None,
    }


def _process_result(
    *,
    backend: str,
    command: str,
    cwd: str | None,
    raw: dict[str, Any],
    max_output_chars: int,
) -> dict[str, Any]:
    status = str(raw.get("status") or "error")
    if status == "completed" and raw.get("exit_code") not in (0, None):
        status = "failed"
    if status == "failed" and not raw.get("error"):
        raw["error"] = f"Command exited with status {raw.get('exit_code')}."
    return _result(
        backend=backend,
        command=command,
        cwd=cwd,
        status=status,
        exit_code=raw.get("exit_code"),
        stdout=str(raw.get("stdout") or raw.get("output") or ""),
        stderr=str(raw.get("stderr") or ""),
        error=raw.get("error"),
        max_output_chars=max_output_chars,
    )


def _terminate_process(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            process.kill()
        except (OSError, AttributeError):
            pass


def _run_process_sync(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: str | None = None,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a backend process with no stdin and process-group timeout cleanup."""

    process = None
    try:
        process = subprocess.Popen(
            list(argv) if not shell else argv,
            shell=shell,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return {
            "status": "completed",
            "exit_code": process.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _terminate_process(process)
            stdout, stderr = process.communicate()
        else:
            stdout, stderr = exc.output or "", exc.stderr or ""
        return {
            "status": "timeout",
            "exit_code": None,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "error": f"Command exceeded the {timeout_seconds:g}-second timeout and was terminated.",
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": "The selected command backend executable is unavailable.",
        }
    except OSError:
        return {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": "The selected command backend could not be started.",
        }


class CommandBackend:
    """Common async interface implemented by each execution backend."""

    name = "unknown"

    def __init__(self, config: CommandBackendConfig):
        self.config = config

    async def execute(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | int | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Alias useful to callers that treat a backend as a command runner."""

        return await self.execute(command, **kwargs)

    def _validate(self, command: str, timeout_seconds: float | int | None) -> tuple[str | None, float | None, dict[str, Any] | None]:
        command, command_error = _validate_command(command, self.config)
        timeout, timeout_error = _bounded_timeout(timeout_seconds, self.config)
        if command_error or timeout_error:
            try:
                max_output = max(1_000, int(self.config.max_output_chars))
            except (TypeError, ValueError):
                max_output = COMMAND_EXECUTION_MAX_OUTPUT_CHARS
            return command, timeout, _result(
                backend=self.name,
                command=command or str(command),
                cwd=None,
                status="error",
                exit_code=None,
                error=command_error or timeout_error,
                max_output_chars=max_output,
            )
        return command, timeout, None

    def _max_output(self) -> int:
        try:
            return max(1_000, int(self.config.max_output_chars))
        except (TypeError, ValueError):
            return COMMAND_EXECUTION_MAX_OUTPUT_CHARS

    @staticmethod
    def _launcher_environment() -> dict[str, str]:
        """Give backend launchers a minimal environment without app secrets."""
        return {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.getenv("LANG", "C.UTF-8"),
            "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
        }


class LocalCommandBackend(CommandBackend):
    """Execute on the Emery host using the existing bounded local runner."""

    name = "local"

    async def execute(self, command: str, *, cwd: str | None = None, timeout_seconds: float | int | None = None) -> dict[str, Any]:
        command, timeout, validation_error = self._validate(command, timeout_seconds)
        if validation_error:
            return validation_error
        assert command is not None and timeout is not None
        raw_cwd = cwd if cwd is not None else (self.config.cwd or COMMAND_EXECUTION_CWD)
        try:
            resolved_cwd = _resolve_working_directory(raw_cwd)
        except (OSError, RuntimeError) as exc:
            return _result(
                backend=self.name,
                command=command,
                cwd=str(raw_cwd),
                status="error",
                exit_code=None,
                error=f"Invalid working directory: {exc}",
                max_output_chars=self._max_output(),
            )
        if not resolved_cwd.is_dir():
            return _result(
                backend=self.name,
                command=command,
                cwd=str(resolved_cwd),
                status="error",
                exit_code=None,
                error="Working directory is not a directory.",
                max_output_chars=self._max_output(),
            )
        raw = await asyncio.to_thread(_run_command_sync, command, resolved_cwd, timeout)
        return _process_result(
            backend=self.name,
            command=command,
            cwd=str(resolved_cwd),
            raw=raw,
            max_output_chars=self._max_output(),
        )


class DockerCommandBackend(CommandBackend):
    """Run a command in a fresh, hardened Docker container."""

    name = "docker"

    def _argv(self, command: str, cwd: str | None) -> list[str]:
        argv = [
            self.config.docker_binary,
            "run",
            "--rm",
            "--init",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--cpus=1",
            "--memory=512m",
            "--network=none",
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        ]
        if cwd:
            argv.extend(["--workdir", cwd])
        argv.extend([self.config.docker_image, "/bin/sh", "-lc", command])
        return argv

    async def execute(self, command: str, *, cwd: str | None = None, timeout_seconds: float | int | None = None) -> dict[str, Any]:
        command, timeout, validation_error = self._validate(command, timeout_seconds)
        if validation_error:
            return validation_error
        assert command is not None and timeout is not None
        target_cwd = cwd if cwd is not None else self.config.cwd
        if target_cwd is not None and not str(target_cwd).strip():
            return _result(
                backend=self.name,
                command=command,
                cwd=target_cwd,
                status="error",
                exit_code=None,
                error="cwd must not be empty.",
                max_output_chars=self._max_output(),
            )
        if not str(self.config.docker_binary).strip() or not str(self.config.docker_image).strip():
            return _result(
                backend=self.name,
                command=command,
                cwd=target_cwd,
                status="error",
                exit_code=None,
                error="Docker binary and image are required.",
                max_output_chars=self._max_output(),
            )
        raw = await asyncio.to_thread(
            _run_process_sync,
            self._argv(command, str(target_cwd) if target_cwd is not None else None),
            timeout_seconds=timeout,
            env=self._launcher_environment(),
        )
        return _process_result(
            backend=self.name,
            command=command,
            cwd=str(target_cwd) if target_cwd is not None else None,
            raw=raw,
            max_output_chars=self._max_output(),
        )


class SSHCommandBackend(CommandBackend):
    """Run a command on a configured host through the system ssh client."""

    name = "ssh"

    def _argv(self, command: str, cwd: str | None, timeout: float) -> list[str]:
        target = f"{self.config.ssh_user}@{self.config.ssh_host}" if self.config.ssh_user else str(self.config.ssh_host)
        argv = [
            self.config.ssh_binary,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            f"ConnectTimeout={max(1, math.ceil(timeout))}",
        ]
        if self.config.ssh_port is not None:
            argv.extend(["-p", str(self.config.ssh_port)])
        if self.config.ssh_key:
            argv.extend(["-i", os.path.expanduser(self.config.ssh_key)])
        remote_command = command
        if cwd is not None:
            remote_command = f"cd -- {shlex.quote(str(cwd))} && {command}"
        argv.extend([target, remote_command])
        return argv

    async def execute(self, command: str, *, cwd: str | None = None, timeout_seconds: float | int | None = None) -> dict[str, Any]:
        command, timeout, validation_error = self._validate(command, timeout_seconds)
        if validation_error:
            return validation_error
        assert command is not None and timeout is not None
        if not self.config.ssh_host or not str(self.config.ssh_host).strip():
            return _result(
                backend=self.name,
                command=command,
                cwd=cwd if cwd is not None else self.config.cwd,
                status="error",
                exit_code=None,
                error="SSH host is required.",
                max_output_chars=self._max_output(),
            )
        if not str(self.config.ssh_binary).strip():
            return _result(
                backend=self.name,
                command=command,
                cwd=cwd if cwd is not None else self.config.cwd,
                status="error",
                exit_code=None,
                error="SSH binary is required.",
                max_output_chars=self._max_output(),
            )
        target_cwd = cwd if cwd is not None else self.config.cwd
        raw = await asyncio.to_thread(
            _run_process_sync,
            self._argv(command, target_cwd, timeout),
            timeout_seconds=timeout,
            env=self._launcher_environment(),
        )
        return _process_result(
            backend=self.name,
            command=command,
            cwd=target_cwd,
            raw=raw,
            max_output_chars=self._max_output(),
        )


def create_command_backend(
    backend: str | None = None,
    *,
    config: CommandBackendConfig | None = None,
    **config_overrides: Any,
) -> CommandBackend:
    """Construct exactly one explicitly selected backend.

    ``config_overrides`` is intentionally kept here rather than reading
    environment variables; the parent integration can decide how Emery's
    configuration should map onto this boundary.
    """

    selected = backend or (config.backend if config else "local")
    if not isinstance(selected, str) or selected.strip().lower() not in SUPPORTED_BACKENDS:
        raise ValueError(f"Unsupported command backend: {selected!r}")
    selected = selected.strip().lower()
    effective = config or CommandBackendConfig(backend=selected)
    if effective.backend != selected:
        effective = replace(effective, backend=selected)
    if config_overrides:
        if "backend" in config_overrides:
            raise ValueError("backend must be selected with the backend argument or config")
        effective = replace(effective, **config_overrides)

    if selected == "local":
        return LocalCommandBackend(effective)
    if selected == "docker":
        return DockerCommandBackend(effective)
    return SSHCommandBackend(effective)


async def run_command(
    command: str,
    *,
    backend: str | None = None,
    config: CommandBackendConfig | None = None,
    cwd: str | None = None,
    timeout_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for one command using an explicitly selected backend."""

    runner = create_command_backend(backend, config=config)
    return await runner.execute(command, cwd=cwd, timeout_seconds=timeout_seconds)


__all__ = [
    "CommandBackend",
    "CommandBackendConfig",
    "DockerCommandBackend",
    "LocalCommandBackend",
    "SSHCommandBackend",
    "SUPPORTED_BACKENDS",
    "create_command_backend",
    "run_command",
]
