"""Telegram approval bridge for commands that Emery's safety guard flags."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from emery.config import BASE_DIR
from emery.telegram_utils import normalize_message_thread_id


@dataclass
class PendingCommandApproval:
    approval_id: str
    command: str
    reason: str
    chat_id: int
    user_id: int | None
    thread_id: int | None
    future: asyncio.Future
    expires_at: float
    approval_key: str | None = None
    message_id: int | None = None


_PENDING_APPROVALS: dict[str, PendingCommandApproval] = {}
_SESSION_APPROVALS: set[tuple[str, str, str, str]] = set()
_FOREVER_APPROVALS: set[tuple[str, str]] = set()
_FOREVER_APPROVALS_LOADED = False
_FOREVER_APPROVALS_LOCK = threading.Lock()
_FOREVER_APPROVALS_PATH = Path(
    os.getenv("APPROVAL_GRANTS_PATH", str(BASE_DIR / "config" / "approval_grants.json"))
).expanduser()

APPROVAL_DENY = "deny"
APPROVAL_ONCE = "once"
APPROVAL_SESSION = "session"
APPROVAL_FOREVER = "forever"
_APPROVAL_DECISIONS = {APPROVAL_DENY, APPROVAL_ONCE, APPROVAL_SESSION, APPROVAL_FOREVER}


def _new_approval_id() -> str:
    return uuid.uuid4().hex[:12]


def _approval_markup(approval_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⛔ Deny", callback_data=f"command_approval:{APPROVAL_DENY}:{approval_id}"),
            InlineKeyboardButton("✅ Approve once", callback_data=f"command_approval:{APPROVAL_ONCE}:{approval_id}"),
        ],
        [
            InlineKeyboardButton("🕒 Approve for session", callback_data=f"command_approval:{APPROVAL_SESSION}:{approval_id}"),
            InlineKeyboardButton("♾️ Approve forever", callback_data=f"command_approval:{APPROVAL_FOREVER}:{approval_id}"),
        ],
    ])


def _normalize_approval_key(approval_key: str | None, command: str) -> str:
    key = str(approval_key or "").strip().casefold()
    if key:
        return key[:160]
    root = str(command or "").strip().split(maxsplit=1)[0].casefold() or "operation"
    return f"generic:{root}"[:160]


def _owner_key(user_id: int | None) -> str:
    return str(user_id) if user_id is not None else "unknown"


def _session_key(chat_id: int, thread_id: int | None, user_id: int | None, approval_key: str) -> tuple[str, str, str, str]:
    return (str(chat_id), str(thread_id) if thread_id is not None else "none", _owner_key(user_id), approval_key)


def _load_forever_approvals() -> None:
    global _FOREVER_APPROVALS_LOADED
    if _FOREVER_APPROVALS_LOADED:
        return
    with _FOREVER_APPROVALS_LOCK:
        if _FOREVER_APPROVALS_LOADED:
            return
        try:
            with _FOREVER_APPROVALS_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            grants = payload.get("grants", []) if isinstance(payload, dict) else []
            for item in grants:
                if not isinstance(item, dict):
                    continue
                user_id = str(item.get("user_id") or "").strip()
                approval_key = str(item.get("approval_key") or "").strip().casefold()
                if user_id and approval_key:
                    _FOREVER_APPROVALS.add((user_id, approval_key))
        except FileNotFoundError:
            pass
        except Exception as exc:
            logging.warning("COMMAND APPROVAL: could not load persistent grants: %s", exc)
        _FOREVER_APPROVALS_LOADED = True


def _save_forever_approvals() -> bool:
    payload = {
        "version": 1,
        "grants": [
            {"user_id": user_id, "approval_key": approval_key}
            for user_id, approval_key in sorted(_FOREVER_APPROVALS)
        ],
    }
    try:
        _FOREVER_APPROVALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=_FOREVER_APPROVALS_PATH.parent,
            prefix=f".{_FOREVER_APPROVALS_PATH.name}.", suffix=".tmp", delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary_path = Path(handle.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, _FOREVER_APPROVALS_PATH)
        return True
    except Exception as exc:
        logging.warning("COMMAND APPROVAL: could not save persistent grants: %s", exc)
        try:
            temporary_path.unlink(missing_ok=True)
        except (NameError, OSError):
            pass
        return False


def _stored_decision(
    *, chat_id: int, thread_id: int | None, user_id: int | None, approval_key: str,
) -> str | None:
    _load_forever_approvals()
    if (_owner_key(user_id), approval_key) in _FOREVER_APPROVALS:
        return APPROVAL_FOREVER
    if _session_key(chat_id, thread_id, user_id, approval_key) in _SESSION_APPROVALS:
        return APPROVAL_SESSION
    return None


def clear_session_approvals(chat_id: int, thread_id: int | None, user_id: int | None = None) -> int:
    """Clear session-scoped grants for one chat/thread/user context."""
    chat_key = str(chat_id)
    thread_key = str(thread_id) if thread_id is not None else "none"
    owner_key = _owner_key(user_id) if user_id is not None else None
    removed = 0
    for key in list(_SESSION_APPROVALS):
        if key[0] != chat_key or key[1] != thread_key:
            continue
        if owner_key is not None and key[2] != owner_key:
            continue
        _SESSION_APPROVALS.remove(key)
        removed += 1
    return removed


def _approval_text(
    command: str,
    reason: str,
    approval_id: str,
    justification: str = "",
    approval_key: str | None = None,
    *,
    state: str = "pending",
) -> str:
    if state == "approved":
        heading = "✅ <b>Command approved</b>"
        footer = "The command was allowed once."
    elif state == "denied":
        heading = "⛔ <b>Command denied</b>"
        footer = "The command was not executed."
    elif state == "expired":
        heading = "⌛ <b>Command approval expired</b>"
        footer = "No decision arrived before the approval window closed. The command was not executed."
    else:
        heading = "⚠️ <b>Command approval required</b>"
        footer = "Approve only if you intended this exact command."

    return (
        f"{heading}\n\n"
        f"<b>Safety reason:</b> {html.escape(str(reason))}\n"
        f"<b>Why Emery needs it:</b> {html.escape(str(justification or 'This command requires an approval check before it can run.'))}\n\n"
        f"<b>ID:</b> <code>{html.escape(approval_id)}</code>\n\n"
        f"<b>Approval scope:</b> <code>{html.escape(str(approval_key or 'this operation'))}</code>\n\n"
        f"<b>Exact command:</b>\n"
        f"<pre>{html.escape(str(command)[:2400])}</pre>\n"
        f"{html.escape(footer)}\n\n"
        "Choose Deny, Approve once, Approve for this session, or Approve forever."
    )


def _approval_status_text(approval: PendingCommandApproval, *, justification: str = "") -> str:
    """Plain-text approval content suitable for the shared status stack."""
    reason = str(approval.reason or "safety policy check").strip()
    why = str(justification or "This command requires an approval check before it can run.").strip()
    command = str(approval.command or "").strip()
    if len(command) > 1800:
        command = command[:1799].rstrip() + "…"
    state_label = "🖥️ Browser control" if str(approval.approval_key or "").startswith("browser:") else "⌨️ Terminal"
    return (
        f"{state_label} approval required\n"
        f"Safety reason: {reason}\n"
        f"Why Emery needs it: {why}\n"
        f"Approval scope: {approval.approval_key or 'this operation'}\n"
        "Exact command:\n"
        f"$ {command}\n\n"
        "Choose Deny, Approve once, Approve for this session, or Approve forever."
    )


def _status_controller(approval: PendingCommandApproval):
    """Return the live chat status controller for this approval, if present."""
    try:
        from emery import globals

        controllers = getattr(globals, "persistent_status_controllers", {})
        key = (
            int(approval.chat_id),
            normalize_message_thread_id(approval.chat_id, approval.thread_id),
        )
        return controllers.get(key)
    except Exception:
        return None


async def _remove_approval_message(approval: PendingCommandApproval) -> None:
    """Remove the approval prompt once its one-shot decision is settled."""
    if approval.message_id is None:
        return
    try:
        from emery import globals

        bot = globals.application_bot
        if bot is None:
            return
        await bot.delete_message(chat_id=approval.chat_id, message_id=approval.message_id)
    except Exception as exc:
        logging.debug("COMMAND APPROVAL: could not remove approval message %s: %s", approval.approval_id, exc)


async def request_command_approval(
    command: str,
    reason: str,
    *,
    chat_id: int | None,
    user_id: int | None,
    thread_id: int | None,
    timeout_seconds: float,
    justification: str = "",
    approval_key: str | None = None,
) -> dict[str, Any]:
    """Wait for a scoped approval decision and fail closed on every error."""
    if chat_id is None:
        return {"approved": False, "status": "unavailable", "message": "No Telegram chat is available for approval."}

    from emery import globals

    bot = globals.application_bot
    if bot is None:
        return {"approved": False, "status": "unavailable", "message": "No Telegram bot is available for approval."}

    normalized_key = _normalize_approval_key(approval_key, command)
    stored_decision = _stored_decision(
        chat_id=int(chat_id), thread_id=thread_id, user_id=user_id, approval_key=normalized_key,
    )
    if stored_decision is not None:
        logging.info(
            "COMMAND APPROVAL: automatically allowed approval_key=%s scope=%s chat_id=%s user_id=%s",
            normalized_key, stored_decision, chat_id, user_id,
        )
        return {
            "approved": True,
            "status": f"approved_{stored_decision}",
            "message": f"approved {stored_decision}",
        }

    loop = asyncio.get_running_loop()
    approval_id = _new_approval_id()
    approval = PendingCommandApproval(
        approval_id=approval_id,
        command=command,
        reason=reason,
        chat_id=int(chat_id),
        user_id=user_id,
        thread_id=thread_id,
        future=loop.create_future(),
        expires_at=time.monotonic() + max(1.0, float(timeout_seconds)),
        approval_key=normalized_key,
    )
    _PENDING_APPROVALS[approval_id] = approval

    controller = _status_controller(approval)
    using_status_stack = controller is not None and not getattr(controller, "disabled", False)
    logging.info(
        "COMMAND APPROVAL: requesting approval_id=%s chat_id=%s thread_id=%s transport=%s",
        approval_id,
        approval.chat_id,
        approval.thread_id,
        "status_stack" if using_status_stack else "direct_message",
    )
    try:
        if controller is not None:
            await controller.set_slot(
                "approval",
                _approval_status_text(approval, justification=justification),
                reply_markup=_approval_markup(approval_id),
                mode="browser" if normalized_key.startswith("browser:") else "terminal",
            )
            # Telegram delivery errors are contained by the status object. If
            # it became disabled while rendering, do not fall back to a fourth
            # Telegram message outside the fixed three-message status block.
            if getattr(controller, "disabled", False):
                raise RuntimeError("persistent status stack is unavailable")
            using_status_stack = True
        else:
            message = await bot.send_message(
                chat_id=approval.chat_id,
                text=_approval_text(command, reason, approval_id, justification, normalized_key),
                parse_mode="HTML",
                reply_markup=_approval_markup(approval_id),
                message_thread_id=thread_id,
            )
            approval.message_id = getattr(message, "message_id", None)
        logging.info(
            "COMMAND APPROVAL: prompt delivered approval_id=%s transport=%s message_id=%s",
            approval_id,
            "status_stack" if using_status_stack else "direct_message",
            approval.message_id,
        )
    except Exception as exc:
        _PENDING_APPROVALS.pop(approval_id, None)
        logging.warning("COMMAND APPROVAL: failed to send approval request %s: %s", approval_id, exc)
        return {"approved": False, "status": "unavailable", "message": "The approval request could not be delivered."}

    state = "denied"
    try:
        decision = await asyncio.wait_for(
            asyncio.shield(approval.future),
            timeout=max(1.0, float(timeout_seconds)),
        )
        decision = str(decision or APPROVAL_DENY).strip().casefold()
        approved = decision in {APPROVAL_ONCE, APPROVAL_SESSION, APPROVAL_FOREVER}
        state = decision if approved else APPROVAL_DENY
        logging.info(
            "COMMAND APPROVAL: resolved approval_id=%s status=%s",
            approval_id,
            state,
        )
        return {
            "approved": approved,
            "status": f"approved_{decision}" if approved else "denied",
            "message": f"approved {decision}" if approved else "denied by the user",
        }
    except asyncio.TimeoutError:
        state = "expired"
        logging.warning("COMMAND APPROVAL: expired approval_id=%s", approval_id)
        return {
            "approved": False,
            "status": "expired",
            "message": "approval timed out; the command was not executed",
        }
    finally:
        _PENDING_APPROVALS.pop(approval_id, None)
        if using_status_stack:
            try:
                await controller.clear_slot("approval")
            except Exception as exc:
                logging.debug("COMMAND APPROVAL: could not clear shared approval status %s: %s", approval_id, exc)
        else:
            await _remove_approval_message(approval)


def resolve_command_approval(
    approval_id: str,
    *,
    approved: bool | None = None,
    decision: str | None = None,
    actor_user_id: int | None,
) -> dict[str, str]:
    """Resolve one pending approval, enforcing that only its requester may answer."""
    approval = _PENDING_APPROVALS.get(str(approval_id or "").strip())
    if approval is None:
        return {"status": "expired", "message": "That command approval is no longer pending."}
    if approval.user_id is None or actor_user_id is None or actor_user_id != approval.user_id:
        return {"status": "unauthorized", "message": "Only the user who requested this command can approve it."}
    if time.monotonic() >= approval.expires_at:
        _PENDING_APPROVALS.pop(approval.approval_id, None)
        if not approval.future.done():
            approval.future.set_result(False)
        return {"status": "expired", "message": "That command approval has expired."}
    if approval.future.done():
        return {"status": "already_resolved", "message": "That command approval was already resolved."}
    selected = str(decision or (APPROVAL_ONCE if approved else APPROVAL_DENY)).strip().casefold()
    if selected not in _APPROVAL_DECISIONS:
        return {"status": "error", "message": "Unknown approval choice."}

    if selected == APPROVAL_SESSION and approval.approval_key:
        _SESSION_APPROVALS.add(_session_key(
            approval.chat_id, approval.thread_id, approval.user_id, approval.approval_key,
        ))
    elif selected == APPROVAL_FOREVER and approval.approval_key:
        _load_forever_approvals()
        with _FOREVER_APPROVALS_LOCK:
            _FOREVER_APPROVALS.add((_owner_key(approval.user_id), approval.approval_key))
            if not _save_forever_approvals():
                selected = APPROVAL_ONCE

    approval.future.set_result(selected)
    return {"status": "resolved", "message": f"Approval response recorded: {selected}."}


async def handle_command_approval_callback(update, context) -> None:
    query = update.callback_query
    if query is None:
        return
    data = str(query.data or "")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "command_approval" or parts[1] not in _APPROVAL_DECISIONS:
        await query.answer("Unknown approval request.", show_alert=True)
        return

    result = resolve_command_approval(
        parts[2],
        decision=parts[1],
        actor_user_id=getattr(query.from_user, "id", None),
    )
    if result["status"] == "resolved":
        await query.answer("Approval response recorded.")
    else:
        await query.answer(result["message"], show_alert=True)


async def handle_command_approval_command(update, context) -> None:
    message = update.message
    if message is None:
        return
    args = list(getattr(context, "args", None) or [])
    approval_id = args[0].strip() if args else ""
    command_name = str(message.text or "").split(maxsplit=1)[0].lstrip("/").split("@", 1)[0].lower()
    if not approval_id:
        await message.reply_text(f"Usage: /{command_name} <approval-id>")
        return

    result = resolve_command_approval(
        approval_id,
        decision=APPROVAL_ONCE if command_name == "approve" else APPROVAL_DENY,
        actor_user_id=getattr(update.effective_user, "id", None),
    )
    await message.reply_text(result["message"])


def pending_command_approval_count() -> int:
    return len(_PENDING_APPROVALS)
