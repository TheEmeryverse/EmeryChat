"""Standalone persistent approval queue for proposed skill writes.

This module deliberately keeps the low-level queue independent from Emery's
tool registry. Importing it, or constructing :class:`SkillApprovalStore`, does
not create files or change application state. The ``SkillApprovalStore``
methods only record decisions; the module-level Emery adapter explicitly
applies an approved change through the normal scoped skill APIs.

The store records proposals only.  Approving an item does not perform the
underlying skill operation; a caller must read the approved record and apply
it through its own integration layer.
"""

from __future__ import annotations

import copy
import difflib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


OPERATIONS = frozenset({
    "create",
    "update",
    "archive",
    "delete",
    "write_file",
    "remove_file",
})
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STATUSES = frozenset({PENDING, APPROVED, REJECTED})

STORE_VERSION = 1
DEFAULT_MAX_CONTENT_BYTES = 128 * 1024
DEFAULT_MAX_ITEMS = 1_000
MAX_ID_LENGTH = 80
MAX_SKILL_ID_LENGTH = 128
MAX_PATH_LENGTH = 512
MAX_SUMMARY_BYTES = 8 * 1024
MAX_ACTOR_BYTES = 256
MAX_NOTE_BYTES = 8 * 1024
MAX_METADATA_BYTES = 16 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_DEFAULT_PATH = Path(
    os.getenv("SKILL_APPROVAL_STORE_PATH", "data/skills/skill_approvals.json")
).expanduser()
_STORE_LOCK = threading.RLock()
_LOGGER = logging.getLogger(__name__)


def _auto_approve_enabled() -> bool:
    """Read the opt-in automatic decision flag without importing app config."""
    return str(os.getenv("SKILL_AUTO_APPROVE", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }


class SkillApprovalError(Exception):
    """Base error for invalid or unavailable approval-store operations."""


class InvalidApprovalError(SkillApprovalError, ValueError):
    """Raised when a proposal does not meet the store's safety constraints."""


class ApprovalNotFoundError(SkillApprovalError, KeyError):
    """Raised when an approval ID is not present in the store."""


class ApprovalStateError(SkillApprovalError, ValueError):
    """Raised when a terminal approval is approved or rejected again."""


class ApprovalStoreCorruptError(SkillApprovalError):
    """Raised when the on-disk JSON is not a valid approval store."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, *, field: str, limit: int, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise InvalidApprovalError(f"{field} must be a string")
    if "\x00" in value:
        raise InvalidApprovalError(f"{field} must not contain NUL bytes")
    if len(value.encode("utf-8")) > limit:
        raise InvalidApprovalError(f"{field} exceeds the {limit}-byte limit")
    return value


def _safe_id(value: Any, *, field: str, max_length: int = MAX_ID_LENGTH) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidApprovalError(f"{field} must be a non-empty string")
    if len(value) > max_length or not _SAFE_ID.fullmatch(value):
        raise InvalidApprovalError(
            f"{field} must contain only letters, numbers, '.', '_' or '-' "
            f"and be at most {max_length} characters"
        )
    return value


def _safe_skill_id(value: Any) -> str:
    return _safe_id(value, field="skill_id", max_length=MAX_SKILL_ID_LENGTH)


def _safe_relative_path(value: Any) -> str:
    path = _text(value, field="target_path", limit=MAX_PATH_LENGTH, allow_none=False)
    assert path is not None
    if not path or path.startswith(("/", "\\")) or "\\" in path:
        raise InvalidApprovalError("target_path must be a non-empty relative POSIX path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidApprovalError("target_path contains an unsafe path component")
    return path


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidApprovalError("metadata must be a mapping")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise InvalidApprovalError("metadata must contain JSON-compatible values") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidApprovalError(f"metadata exceeds the {MAX_METADATA_BYTES}-byte limit")
    if not isinstance(normalized, dict):
        raise InvalidApprovalError("metadata must serialize to a JSON object")
    return normalized


def _new_id() -> str:
    # URL-safe characters are all accepted by _SAFE_ID.
    return "sa_" + secrets.token_urlsafe(18).rstrip("=")


class SkillApprovalStore:
    """A small JSON-backed queue of proposed skill mutations.

    Parameters are intentionally local to this store instance.  The file is
    read lazily, and is created only by a successful mutating method.  All
    returned records are copies, so a caller cannot mutate the in-memory value
    without explicitly writing another decision.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> None:
        if not isinstance(max_content_bytes, int) or max_content_bytes <= 0:
            raise ValueError("max_content_bytes must be a positive integer")
        if not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        self.path = Path(path).expanduser() if path is not None else _DEFAULT_PATH
        self.max_content_bytes = max_content_bytes
        self.max_items = max_items

    def stage(
        self,
        operation: str,
        *,
        skill_id: str,
        before: str | None = None,
        after: str | None = None,
        target_path: str | None = None,
        summary: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        requested_by: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a new pending proposal and return its record.

        ``before`` and ``after`` are optional text snapshots used for review.
        ``after`` is required for ``create``, ``update``, and ``write_file``;
        file operations additionally require ``target_path``.  The store does
        not infer or execute a mutation from these fields.
        """
        operation = _text(operation, field="operation", limit=32, allow_none=False)
        assert operation is not None
        operation = operation.strip().lower()
        if operation not in OPERATIONS:
            raise InvalidApprovalError(f"unsupported operation: {operation!r}")
        skill_id = _safe_skill_id(skill_id)
        if operation in {"write_file", "remove_file"}:
            target_path = _safe_relative_path(target_path)
        elif target_path is not None:
            raise InvalidApprovalError(f"target_path is only valid for file operations")
        if operation in {"create", "update", "write_file"} and after is None:
            raise InvalidApprovalError(f"{operation} requires after content")
        before = _text(
            before,
            field="before",
            limit=self.max_content_bytes,
        )
        after = _text(
            after,
            field="after",
            limit=self.max_content_bytes,
        )
        summary = _text(summary, field="summary", limit=MAX_SUMMARY_BYTES)
        requested_by = _text(requested_by, field="requested_by", limit=MAX_ACTOR_BYTES)
        metadata = _metadata(metadata)
        if approval_id is None:
            approval_id = _new_id()
        else:
            approval_id = _safe_id(approval_id, field="approval_id")

        if summary is None:
            subject = target_path or skill_id
            summary = f"Proposed {operation} for {subject}"
        timestamp = _now()
        record = {
            "id": approval_id,
            "operation": operation,
            "status": PENDING,
            "skill_id": skill_id,
            "target_path": target_path,
            "before": before,
            "after": after,
            "summary": summary,
            "metadata": metadata,
            "requested_by": requested_by,
            "created_at": timestamp,
            "updated_at": timestamp,
            "decided_at": None,
            "decided_by": None,
            "decision_note": None,
        }
        with _STORE_LOCK:
            payload = self._load()
            items = payload["items"]
            if approval_id in items:
                raise InvalidApprovalError(f"approval_id already exists: {approval_id}")
            if len(items) >= self.max_items:
                raise InvalidApprovalError(f"approval store is full ({self.max_items} items)")
            items[approval_id] = record
            self._save(payload)
        return copy.deepcopy(record)

    def list_pending(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return pending records, oldest first, without changing the store."""
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer or None")
        with _STORE_LOCK:
            records = [item for item in self._load()["items"].values() if item["status"] == PENDING]
        records.sort(key=lambda item: (item["created_at"], item["id"]))
        if limit is not None:
            records = records[:limit]
        return copy.deepcopy(records)

    def read(self, approval_id: str) -> dict[str, Any]:
        """Read one proposal by safe ID."""
        approval_id = _safe_id(approval_id, field="approval_id")
        with _STORE_LOCK:
            record = self._load()["items"].get(approval_id)
        if record is None:
            raise ApprovalNotFoundError(approval_id)
        return copy.deepcopy(record)

    def diff(self, approval_id: str) -> str:
        """Return a bounded, unified-style human-readable proposal summary."""
        record = self.read(approval_id)
        lines = [
            f"Approval {record['id']}",
            f"Operation: {record['operation']}",
            f"Status: {record['status']}",
            f"Skill: {record['skill_id']}",
        ]
        if record.get("target_path"):
            lines.append(f"Target: {record['target_path']}")
        lines.append(f"Summary: {record['summary']}")
        before = record.get("before")
        after = record.get("after")
        if before is None and after is None:
            lines.append("Content: no text snapshot supplied")
            return "\n".join(lines)
        old_label = f"a/{record.get('target_path') or record['skill_id']}"
        new_label = f"b/{record.get('target_path') or record['skill_id']}"
        old_lines = (before or "").splitlines(keepends=True)
        new_lines = (after or "").splitlines(keepends=True)
        lines.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=old_label,
                tofile=new_label,
                lineterm="",
            )
        )
        return "\n".join(lines)

    def approve(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Mark a pending proposal approved; the low-level store does not execute it."""
        return self._decide(approval_id, APPROVED, decided_by=decided_by, note=note)

    def reject(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Mark a pending proposal rejected; does not execute it."""
        return self._decide(approval_id, REJECTED, decided_by=decided_by, note=note)

    def _decide(
        self,
        approval_id: str,
        status: str,
        *,
        decided_by: str | None,
        note: str | None,
    ) -> dict[str, Any]:
        approval_id = _safe_id(approval_id, field="approval_id")
        decided_by = _text(decided_by, field="decided_by", limit=MAX_ACTOR_BYTES)
        note = _text(note, field="decision_note", limit=MAX_NOTE_BYTES)
        with _STORE_LOCK:
            payload = self._load()
            record = payload["items"].get(approval_id)
            if record is None:
                raise ApprovalNotFoundError(approval_id)
            if record["status"] != PENDING:
                raise ApprovalStateError(
                    f"approval {approval_id} is already {record['status']}"
                )
            timestamp = _now()
            record.update({
                "status": status,
                "updated_at": timestamp,
                "decided_at": timestamp,
                "decided_by": decided_by,
                "decision_note": note,
            })
            self._save(payload)
            return copy.deepcopy(record)

    def _load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {"version": STORE_VERSION, "items": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ApprovalStoreCorruptError(f"could not read {self.path}: {exc}") from exc
        self._validate_payload(payload)
        return payload

    def _validate_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            raise ApprovalStoreCorruptError("unsupported approval store format")
        items = payload.get("items")
        if not isinstance(items, dict) or len(items) > self.max_items:
            raise ApprovalStoreCorruptError("approval store has an invalid item collection")
        for approval_id, record in items.items():
            try:
                _safe_id(approval_id, field="approval_id")
            except InvalidApprovalError as exc:
                raise ApprovalStoreCorruptError(str(exc)) from exc
            if not isinstance(record, dict) or record.get("id") != approval_id:
                raise ApprovalStoreCorruptError("approval record has an invalid ID")
            if record.get("operation") not in OPERATIONS or record.get("status") not in STATUSES:
                raise ApprovalStoreCorruptError("approval record has an invalid operation or status")
            try:
                _safe_skill_id(record.get("skill_id"))
                _text(record.get("before"), field="before", limit=self.max_content_bytes)
                _text(record.get("after"), field="after", limit=self.max_content_bytes)
                _text(record.get("summary"), field="summary", limit=MAX_SUMMARY_BYTES, allow_none=False)
                _metadata(record.get("metadata"))
                if record.get("target_path") is not None:
                    _safe_relative_path(record["target_path"])
            except InvalidApprovalError as exc:
                raise ApprovalStoreCorruptError(str(exc)) from exc

    def _save(self, payload: dict[str, Any]) -> None:
        self._validate_payload(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            temporary_path = None
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The file replacement is still atomic on platforms without a
                # fsync-able directory handle.
                pass
        except OSError as exc:
            raise SkillApprovalError(f"could not persist approval store: {exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _default_store() -> SkillApprovalStore:
    return SkillApprovalStore()


def _scope_metadata(user_id: int | None, chat_id: int | None) -> dict[str, Any]:
    return {
        "_emery_user_id": int(user_id) if user_id is not None else None,
        "_emery_chat_id": int(chat_id) if chat_id is not None else None,
    }


def _in_scope(record: Mapping[str, Any], *, user_id: int | None, chat_id: int | None) -> bool:
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return False
    scope = metadata.get("_emery_scope") if isinstance(metadata.get("_emery_scope"), Mapping) else metadata
    stored_chat = scope.get("_emery_chat_id")
    stored_user = scope.get("_emery_user_id")
    if stored_chat is not None and chat_id is not None and int(stored_chat) != int(chat_id):
        return False
    if stored_chat is not None and chat_id is None:
        return False
    # Private proposals are user-owned. Group proposals are chat-owned.
    if stored_chat is not None:
        return True
    return stored_user is not None and user_id is not None and int(stored_user) == int(user_id)


def stage_skill_change(
    operation: str,
    *,
    skill_id: str,
    payload: Mapping[str, Any] | None = None,
    before: str | None = None,
    after: str | None = None,
    target_path: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> dict[str, Any]:
    """Stage an Emery skill mutation with its scoped application payload."""
    metadata = dict(payload or {})
    metadata["_emery_scope"] = _scope_metadata(user_id, chat_id)
    record = _default_store().stage(
        operation,
        skill_id=skill_id,
        before=before,
        after=after,
        target_path=target_path,
        metadata=metadata,
        requested_by=str(user_id) if user_id is not None else None,
    )
    if _auto_approve_enabled():
        _LOGGER.info(
            "SKILL APPROVAL: automatically applying staged %s change id=%s skill=%s",
            operation,
            record["id"],
            skill_id,
        )
        result = approve(
            record["id"],
            user_id=user_id,
            chat_id=chat_id,
            note="Automatically approved by SKILL_AUTO_APPROVE",
        )
        result["message"] = "Skill change automatically approved and applied."
        return result
    return {"ok": True, "status": PENDING, "approval": record,
            "message": "Skill change staged for approval."}


def list_pending(*, user_id: int | None = None, chat_id: int | None = None) -> list[dict[str, Any]]:
    return [
        record for record in _default_store().list_pending()
        if _in_scope(record, user_id=user_id, chat_id=chat_id)
    ]


def read_pending(approval_id: str, *, user_id: int | None = None, chat_id: int | None = None) -> dict[str, Any]:
    record = _default_store().read(approval_id)
    if not _in_scope(record, user_id=user_id, chat_id=chat_id):
        raise ApprovalNotFoundError(approval_id)
    return record


def format_diff(pending: Mapping[str, Any]) -> str:
    approval_id = pending.get("id") or pending.get("approval_id")
    if not approval_id:
        raise InvalidApprovalError("pending change is missing an approval ID")
    return _default_store().diff(str(approval_id))


def _apply_record(record: Mapping[str, Any]) -> Any:
    """Apply an approved record through Emery's normal scoped skill APIs."""
    from emery import skills

    metadata = dict(record.get("metadata") or {})
    payload = dict(metadata)
    scope = payload.pop("_emery_scope", {}) or {}
    user_id = scope.get("_emery_user_id")
    chat_id = scope.get("_emery_chat_id")
    operation = record.get("operation")
    skill_id = str(record.get("skill_id"))
    if operation == "create":
        return skills.save_skill(
            payload["name"], payload["description"], payload["procedure"],
            triggers=payload.get("triggers"), prerequisites=payload.get("prerequisites", ""),
            verification=payload.get("verification", ""), failure_modes=payload.get("failure_modes", ""),
            tools=payload.get("tools"), category=payload.get("category"), files=payload.get("files"),
            scope=payload.get("scope"), status=payload.get("status", "draft"),
            user_id=user_id, chat_id=chat_id,
        )
    if operation in {"update", "delete", "archive"}:
        if operation in {"delete", "archive"}:
            return skills.archive_skill(skill_id, user_id=user_id, chat_id=chat_id)
        requested_status = payload.pop("status", None)
        result = skills.update_skill(skill_id, user_id=user_id, chat_id=chat_id, **payload)
        if requested_status:
            result = skills.set_skill_status(skill_id, requested_status, user_id=user_id, chat_id=chat_id)
        return result
    if operation == "write_file":
        return skills.write_skill_file(record["target_path"], record.get("after") or "", identifier=skill_id, user_id=user_id, chat_id=chat_id)
    if operation == "remove_file":
        return skills.remove_skill_file(record["target_path"], identifier=skill_id, user_id=user_id, chat_id=chat_id)
    raise InvalidApprovalError(f"unsupported application operation: {operation}")


def approve(approval_id: str, *, user_id: int | None = None, chat_id: int | None = None, note: str | None = None) -> dict[str, Any]:
    record = read_pending(approval_id, user_id=user_id, chat_id=chat_id)
    result = _apply_record(record)
    decided = _default_store().approve(approval_id, decided_by=str(user_id) if user_id is not None else None, note=note)
    return {**decided, "applied": True, "result": result}


def reject(approval_id: str, *, user_id: int | None = None, chat_id: int | None = None, note: str | None = None) -> dict[str, Any]:
    record = read_pending(approval_id, user_id=user_id, chat_id=chat_id)
    return _default_store().reject(approval_id, decided_by=str(user_id) if user_id is not None else None, note=note)


__all__ = [
    "APPROVED",
    "ApprovalNotFoundError",
    "ApprovalStateError",
    "ApprovalStoreCorruptError",
    "InvalidApprovalError",
    "OPERATIONS",
    "PENDING",
    "REJECTED",
    "SkillApprovalError",
    "SkillApprovalStore",
    "approve",
    "format_diff",
    "list_pending",
    "read_pending",
    "reject",
    "stage_skill_change",
]
