"""Telegram approval bridge for commands that Emery's safety guard flags."""

from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


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
    message_id: int | None = None


_PENDING_APPROVALS: dict[str, PendingCommandApproval] = {}


def _new_approval_id() -> str:
    return uuid.uuid4().hex[:12]


def _approval_markup(approval_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve once", callback_data=f"command_approval:approve:{approval_id}"),
            InlineKeyboardButton("⛔ Deny", callback_data=f"command_approval:deny:{approval_id}"),
        ]
    ])


def _approval_text(command: str, reason: str, approval_id: str, *, state: str = "pending") -> str:
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
        f"<b>Reason:</b> {html.escape(str(reason))}\n"
        f"<b>ID:</b> <code>{html.escape(approval_id)}</code>\n\n"
        f"<pre>{html.escape(str(command)[:2400])}</pre>\n"
        f"{html.escape(footer)}"
    )


async def _edit_approval_message(approval: PendingCommandApproval, state: str) -> None:
    if approval.message_id is None:
        return
    try:
        from emery import globals

        bot = globals.application_bot
        if bot is None:
            return
        await bot.edit_message_text(
            chat_id=approval.chat_id,
            message_id=approval.message_id,
            text=_approval_text(approval.command, approval.reason, approval.approval_id, state=state),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as exc:
        logging.debug("COMMAND APPROVAL: could not update approval message %s: %s", approval.approval_id, exc)


async def request_command_approval(
    command: str,
    reason: str,
    *,
    chat_id: int | None,
    user_id: int | None,
    thread_id: int | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait for an approve-once/deny decision and fail closed on every error."""
    if chat_id is None:
        return {"approved": False, "status": "unavailable", "message": "No Telegram chat is available for approval."}

    from emery import globals

    bot = globals.application_bot
    if bot is None:
        return {"approved": False, "status": "unavailable", "message": "No Telegram bot is available for approval."}

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
    )
    _PENDING_APPROVALS[approval_id] = approval

    try:
        message = await bot.send_message(
            chat_id=approval.chat_id,
            text=_approval_text(command, reason, approval_id),
            parse_mode="HTML",
            reply_markup=_approval_markup(approval_id),
            message_thread_id=thread_id,
        )
        approval.message_id = getattr(message, "message_id", None)
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
        approved = bool(decision)
        state = "approved" if approved else "denied"
        return {
            "approved": approved,
            "status": "approved" if approved else "denied",
            "message": "approved" if approved else "denied by the user",
        }
    except asyncio.TimeoutError:
        state = "expired"
        return {
            "approved": False,
            "status": "expired",
            "message": "approval timed out; the command was not executed",
        }
    finally:
        _PENDING_APPROVALS.pop(approval_id, None)
        await _edit_approval_message(approval, state)


def resolve_command_approval(approval_id: str, *, approved: bool, actor_user_id: int | None) -> dict[str, str]:
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
    approval.future.set_result(bool(approved))
    return {"status": "resolved", "message": "Approval response recorded."}


async def handle_command_approval_callback(update, context) -> None:
    query = update.callback_query
    if query is None:
        return
    data = str(query.data or "")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "command_approval" or parts[1] not in {"approve", "deny"}:
        await query.answer("Unknown approval request.", show_alert=True)
        return

    result = resolve_command_approval(
        parts[2],
        approved=parts[1] == "approve",
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
        approved=command_name == "approve",
        actor_user_id=getattr(update.effective_user, "id", None),
    )
    await message.reply_text(result["message"])


def pending_command_approval_count() -> int:
    return len(_PENDING_APPROVALS)
