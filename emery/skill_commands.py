"""User-facing Telegram commands for Emery's durable skills.

This module keeps command formatting independent from the application
bootstrap.  The optional approval backend is resolved lazily so the command
surface can ship before that backend exists.
"""

from __future__ import annotations

import html
import importlib
import inspect
from collections.abc import Mapping
from typing import Any

from emery.config import ALLOWED_USER_IDS, ALLOW_UNRESTRICTED_TELEGRAM_ACCESS
from emery import skills


MAX_REPLY_CHARS = 3900
MAX_LIST_ITEMS = 25

# The approval store is intentionally not implemented here.  A future approval
# backend can be exposed as ``emery.skill_approval``; tests and deployments may
# inject the object directly.  Every call below receives the current Telegram
# user and chat scope, and scope is never omitted as a fallback.
approval_api: Any | None = None


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _escape_limited(value: Any, limit: int) -> str:
    """Escape dynamic HTML while keeping the escaped result within a budget."""
    text = "" if value is None else str(value).strip()
    escaped = html.escape(text, quote=False)
    if len(escaped) <= limit:
        return escaped
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(html.escape(text[:middle], quote=False)) <= max(0, limit - 1):
            low = middle
        else:
            high = middle - 1
    return html.escape(text[:low].rstrip(), quote=False) + "…"


def _user_allowed(update: Any) -> bool:
    """Mirror Emery's Telegram access policy without importing bot.py."""
    user = getattr(update, "effective_user", None)
    if not user or getattr(user, "is_bot", False):
        return False
    if ALLOWED_USER_IDS:
        return user.id in ALLOWED_USER_IDS
    return bool(ALLOW_UNRESTRICTED_TELEGRAM_ACCESS)


def _scope_ids(update: Any) -> tuple[int, int]:
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    return int(user.id), int(chat.id)


def _args(context: Any) -> list[str]:
    return [str(arg) for arg in (getattr(context, "args", None) or []) if str(arg).strip()]


def _usage() -> str:
    return (
        "<b>Skill commands</b>\n\n"
        "/skills list - List skills visible in this chat.\n"
        "/skills search &lt;query&gt; - Search visible skills.\n"
        "/skills show &lt;id-or-name&gt; - Show a skill and its procedure.\n"
        "/skills status &lt;id-or-name&gt; - Show a skill's lifecycle status.\n"
        "/skills pending - List pending skill changes.\n"
        "/skills diff &lt;id&gt; - Show a pending skill change.\n"
        "/skills approve &lt;id&gt; - Approve a pending skill change.\n"
        "/skills reject &lt;id&gt; - Reject a pending skill change.\n"
        "/skills archive &lt;id-or-name&gt; - Archive a visible skill."
    )


def _skill_label(skill: Mapping[str, Any]) -> str:
    name = _escape(skill.get("name") or skill.get("slug") or skill.get("id"))
    identifier = _escape(skill.get("id") or skill.get("slug") or "")
    status = _escape(skill.get("status") or "active")
    description = _escape_limited(skill.get("description"), 180)
    return f"• <b>{name}</b> <code>{identifier}</code> · {status}\n  {description}"


def _render_skill_list(title: str, items: list[Mapping[str, Any]]) -> str:
    if not items:
        return f"<b>{_escape(title)}</b>\n\nNo matching skills in this scope."

    lines = [f"<b>{_escape(title)}</b>"]
    shown = 0
    for skill in items[:MAX_LIST_ITEMS]:
        line = _skill_label(skill)
        if sum(len(part) + 1 for part in lines) + len(line) > MAX_REPLY_CHARS:
            break
        lines.append(line)
        shown += 1
    remaining = len(items) - shown
    if remaining > 0:
        lines.append(f"\n…and {remaining} more. Use <code>/skills search &lt;query&gt;</code> to narrow the list.")
    return "\n".join(lines)


def _render_skill(skill: Mapping[str, Any], *, status_only: bool = False) -> str:
    name = _escape(skill.get("name") or skill.get("slug") or skill.get("id"))
    identifier = _escape(skill.get("id") or skill.get("slug") or "")
    status = _escape(skill.get("status") or "unknown")
    header = f"🧩 <b>{name}</b> <code>{identifier}</code>"
    if status_only:
        return f"{header}\nStatus: <b>{status}</b>"

    lines = [
        header,
        f"Status: <b>{status}</b> · Version {_escape(skill.get('version') or 1)}",
        f"Scope: {_escape(skill.get('scope') or 'unknown')}",
        "",
        f"<b>Purpose</b>\n{_escape_limited(skill.get('description'), 500)}",
    ]
    triggers = skill.get("triggers") or []
    if triggers:
        lines.append(
            f"<b>Triggers</b>\n{_escape_limited(', '.join(str(item) for item in triggers), 500)}"
        )
    procedure = skill.get("procedure") or skill.get("instructions") or ""
    if procedure:
        lines.append(f"<b>Procedure</b>\n{_escape_limited(procedure, 1600)}")
    verification = skill.get("verification") or ""
    if verification:
        lines.append(f"<b>Verification</b>\n{_escape_limited(verification, 400)}")
    failure_modes = skill.get("failure_modes") or ""
    if failure_modes:
        lines.append(f"<b>Failure modes</b>\n{_escape_limited(failure_modes, 400)}")
    return "\n\n".join(lines)


def _approval_backend() -> Any | None:
    """Resolve the optional future approval API without making it mandatory."""
    if approval_api is not None:
        return approval_api
    try:
        return importlib.import_module("emery.skill_approval")
    except (ImportError, ModuleNotFoundError):
        return None


async def _approval_call(
    operation: str,
    *args: Any,
    user_id: int,
    chat_id: int,
) -> tuple[Any | None, str | None]:
    """Call one approval operation while preserving the Telegram scope.

    The future API contract is that all operations accept ``user_id`` and
    ``chat_id`` keyword arguments.  If the backend is absent, incomplete, or
    rejects the scoped call, fail closed and surface a safe user-facing error;
    never retry the operation without scope.
    """
    backend = _approval_backend()
    if backend is None:
        return None, "Skill approval is not available yet."
    method = getattr(backend, operation, None)
    if not callable(method):
        return None, "Skill approval is not available yet."
    try:
        result = method(*args, user_id=user_id, chat_id=chat_id)
        if inspect.isawaitable(result):
            result = await result
        return result, None
    except Exception:
        # Approval backends must not leak implementation details or cross-scope
        # errors into Telegram.  Log/diagnose at the backend boundary instead.
        return None, "That skill approval request could not be completed."


def _approval_items(value: Any) -> list[Mapping[str, Any]]:
    """Normalize common approval API collection envelopes for rendering."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ("items", "pending", "changes", "results"):
            nested = value.get(key)
            if isinstance(nested, (list, tuple)):
                return [item for item in nested if isinstance(item, Mapping)]
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _pending_label(item: Mapping[str, Any]) -> str:
    identifier = item.get("id") or item.get("pending_id") or item.get("change_id") or "unknown"
    skill = item.get("skill_name") or item.get("name") or item.get("skill_id") or "skill change"
    operation = item.get("operation") or item.get("action") or "change"
    return (
        f"• <b>{_escape_limited(skill, 160)}</b> "
        f"<code>{_escape_limited(identifier, 120)}</code> · {_escape(operation)}"
    )


def _render_pending_changes(value: Any) -> str:
    items = _approval_items(value)
    if not items:
        return "<b>🕒 Pending skill changes</b>\n\nNo pending skill changes in this scope."
    lines = ["<b>🕒 Pending skill changes</b>"]
    for item in items[:MAX_LIST_ITEMS]:
        lines.append(_pending_label(item))
    if len(items) > MAX_LIST_ITEMS:
        lines.append(f"\n…and {len(items) - MAX_LIST_ITEMS} more.")
    return "\n".join(lines)


def _pending_identifier(args: list[str]) -> str | None:
    if len(args) != 2 or not args[1].strip():
        return None
    return args[1].strip()


def _render_approval_result(action: str, value: Any) -> str:
    if isinstance(value, Mapping):
        identifier = value.get("id") or value.get("pending_id") or value.get("change_id") or ""
        name = value.get("skill_name") or value.get("name") or value.get("skill_id") or identifier
        status = value.get("status") or value.get("state")
        suffix = f" · {_escape(status)}" if status else ""
        return f"{action} skill change <b>{_escape_limited(name, 200)}</b>{suffix}."
    return f"{action} skill change <b>{_escape_limited(value or 'requested change', 200)}</b>."


async def _reply(update: Any, text: str) -> None:
    message = getattr(update, "message", None)
    if message is not None:
        await message.reply_text(text, parse_mode="HTML")


async def handle_skills_command(update: Any, context: Any) -> None:
    """Handle ``/skills`` and its read-only/lifecycle subcommands."""
    if not _user_allowed(update):
        return
    if not getattr(update, "message", None) or not getattr(update, "effective_chat", None):
        return

    user_id, chat_id = _scope_ids(update)
    args = _args(context)
    if not args:
        await _reply(update, _usage())
        return

    command = args[0].casefold()
    if command in {"help", "?"}:
        await _reply(update, _usage())
        return

    if command == "list":
        items = skills.list_skills(include_drafts=True, user_id=user_id, chat_id=chat_id)
        await _reply(update, _render_skill_list("🧩 Skills", items))
        return

    if command == "pending":
        pending, error = await _approval_call(
            "list_pending", user_id=user_id, chat_id=chat_id
        )
        await _reply(update, error or _render_pending_changes(pending))
        return

    if command in {"diff", "approve", "reject"}:
        identifier = _pending_identifier(args)
        if identifier is None:
            await _reply(update, f"Usage: <code>/skills {command} &lt;id&gt;</code>")
            return

        if command == "diff":
            pending, error = await _approval_call(
                "read_pending", identifier, user_id=user_id, chat_id=chat_id
            )
            if error:
                await _reply(update, error)
                return
            backend = _approval_backend()
            formatter = getattr(backend, "format_diff", None) if backend is not None else None
            if not callable(formatter):
                await _reply(update, "Skill approval is not available yet.")
                return
            try:
                diff = formatter(pending)
                if inspect.isawaitable(diff):
                    diff = await diff
            except Exception:
                await _reply(update, "That skill approval request could not be completed.")
                return
            rendered = _escape_limited(diff, MAX_REPLY_CHARS - 80)
            await _reply(
                update,
                f"<b>Diff for skill change</b> <code>{_escape(identifier)}</code>\n\n"
                f"<pre>{rendered or '(empty diff)'}</pre>",
            )
            return

        result, error = await _approval_call(
            command, identifier, user_id=user_id, chat_id=chat_id
        )
        if error:
            await _reply(update, error)
            return
        verb = "Approved" if command == "approve" else "Rejected"
        await _reply(update, _render_approval_result(verb, result))
        return

    if command == "search":
        query = " ".join(args[1:]).strip()
        if not query:
            await _reply(update, "Usage: <code>/skills search &lt;query&gt;</code>")
            return
        items = skills.search_skills(
            query, limit=skills.MAX_RETRIEVED_SKILLS * 3, include_drafts=True,
            user_id=user_id, chat_id=chat_id,
        )
        await _reply(update, _render_skill_list(f"🔎 Skills matching “{query}”", items))
        return

    if command in {"show", "status", "archive"}:
        identifier = " ".join(args[1:]).strip()
        if not identifier:
            await _reply(update, f"Usage: <code>/skills {command} &lt;id-or-name&gt;</code>")
            return
        try:
            if command == "archive":
                archived = skills.set_skill_status(identifier, "archived", user_id=user_id, chat_id=chat_id)
                await _reply(update, f"Archived skill <b>{_escape(archived.get('name') or identifier)}</b>.")
                return
            skill = skills.read_skill(
                identifier, include_drafts=True, include_archived=True,
                user_id=user_id, chat_id=chat_id,
            )
        except skills.SkillError as exc:
            await _reply(update, f"Unable to find that skill in this scope: {_escape(exc)}")
            return
        await _reply(update, _render_skill(skill, status_only=command == "status"))
        return

    await _reply(update, _usage())


__all__ = ["handle_skills_command"]
