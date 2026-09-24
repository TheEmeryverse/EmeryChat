import re
import logging
import asyncio
import base64
import html
from datetime import datetime, time
from collections import deque

from telegram import Update, ReplyParameters
from telegram.ext import ContextTypes
from telegram.error import TimedOut

from emery.config import (
    MODEL_ID, MODEL_NAME, USER_TIMEZONE, VISION_MODEL_ID, MAIN_MODEL_VISION, USER_BIRTHDAY,
    ENABLE_HEARTBEAT, HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_SILENCE_THRESHOLD_SECONDS,
    HEARTBEAT_SILENT_RETRY_SECONDS, HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS,
    HEARTBEAT_DAILY_PROACTIVE_LIMIT, HEARTBEAT_SLEEP_START, HEARTBEAT_SLEEP_END,
    ALLOWED_USER_IDS, ALLOWED_BOT_IDS, ENABLE_WEATHER,
    TELEGRAM_GROUP_CHAT_ID, CHAT_TOPIC_ID, TELEGRAM_STICKER_SET,
    ALLOW_UNRESTRICTED_TELEGRAM_ACCESS, ENABLE_LIVE_PROGRESS,
    ENABLE_LIVE_STEERING,
    LIVE_STEERING_MAX_PENDING,
    IMAGE_MAX_BATCH_SIZE,
)
import emery.globals as globals
from emery.helpers import (
    emery_format, transcribe_audio, compress_image_bytes,
    get_image_description, clean_thinking_tags, telegram_escape
)
from emery.session_context import get_session_context, get_turn_context, set_current_context, clear_session_context_cache
from emery.temporary_mode import (
    is_temporary_mode,
    is_chat_temporary_mode,
    set_temporary_mode,
    temporary_history,
)
from emery.logging_utils import safe_preview
from emery.memory import retrieve_relevant_memories, wipe_memory
from emery.scratchpad import clear_scratchpad, read_scratchpad
from emery.engine import emery_engine
from emery.telegram_delivery import (
    TelegramLiveProgress,
    PersistentStatusStack,
    send_rich_html_or_split_html_message,
    send_rich_or_split_html_message,
    send_split_html_message,
)
from emery.docling import (
    detect_supported_document_type,
    convert_document_bytes,
    analyze_pdf_visual_fallback,
    build_extracted_text_preview,
    build_document_context_text,
)
from emery.tools import (
    cancel_image_queue,
    get_noaa_weather_alerts,
    get_voice_audio,
    pause_image_queue,
    queue_image_generation,
    resume_image_queue,
)
from emery.image_lifecycle import defer_active_chat_message, get_active_image_generation
from emery.image_profiles import DIRECT_IMAGE_DEFAULT_PROFILE, get_image_profile, image_profile_batch_limit
from emery.telegram_utils import normalize_message_thread_id


PREFILL_FALLBACK_STATUS = "Preparing response…"

TOOL_DISPLAY_NAMES = {
    "get_noaa_weather": "weather lookup",
    "get_noaa_weather_alerts": "weather alerts lookup",
    "get_stock_snapshot": "stock lookup",
    "web_search": "web search",
    "search_query": "web search",
    "get_voice_audio": "voice generation",
}

COMMAND_STATUS_TOOLS = {
    "run_command", "terminal_exec", "terminal_session_start", "terminal_session_write",
    "terminal_session_read", "terminal_session_close", "terminal_job_start",
    "terminal_job_status", "terminal_job_read", "terminal_job_wait", "terminal_job_cancel",
    "terminal_list_sessions", "terminal_list_jobs",
}
BROWSER_STATUS_TOOLS = {
    "list_browser_tabs", "open_browser_tab", "browser_snapshot", "browser_screenshot",
    "browser_navigate", "browser_click", "browser_type", "browser_press", "browser_back",
    "browser_scroll", "browser_console", "browser_handle_dialog", "close_browser_tab",
    "close_browser", "browser_session_start", "browser_session_status", "browser_session_list",
    "browser_session_open_tab", "browser_session_close", "browser_session_cleanup",
}


def _compact_status_value(value, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def _browser_action_label(tool_name: str, args: dict) -> str:
    args = args or {}
    if tool_name == "list_browser_tabs":
        return "listed open tabs"
    if tool_name == "open_browser_tab":
        return "opened a new tab"
    if tool_name == "browser_snapshot":
        return "inspected the page"
    if tool_name == "browser_screenshot":
        return "captured a screenshot"
    if tool_name == "browser_navigate":
        return "navigated to the page"
    if tool_name == "browser_click":
        return f"clicked {_compact_status_value(args.get('ref'), 60) or 'an element'}"
    if tool_name == "browser_type":
        ref = _compact_status_value(args.get("ref"), 60) or "an input"
        count = len(str(args.get("text") or ""))
        return f"typed {count} character{'s' if count != 1 else ''} into {ref}"
    if tool_name == "browser_press":
        return f"pressed {_compact_status_value(args.get('key'), 60) or 'a key'}"
    if tool_name == "browser_back":
        return "went back"
    if tool_name == "browser_scroll":
        direction = _compact_status_value(args.get("direction"), 20) or "down"
        amount = _compact_status_value(args.get("amount"), 20) or "800"
        return f"scrolled {direction} {amount}px"
    if tool_name == "browser_console":
        return "read the browser console"
    if tool_name == "browser_handle_dialog":
        action = _compact_status_value(args.get("action"), 20) or "handled"
        return f"{action}ed the browser dialog"
    if tool_name == "close_browser_tab":
        return "closed the tab"
    if tool_name == "close_browser":
        return "closed the browser session"
    if tool_name == "browser_session_start":
        return "started a browser session"
    if tool_name == "browser_session_status":
        return "checked browser session status"
    if tool_name == "browser_session_list":
        return "listed browser sessions"
    if tool_name == "browser_session_open_tab":
        return "opened a tab in the browser session"
    if tool_name == "browser_session_close":
        return "closed the browser session"
    if tool_name == "browser_session_cleanup":
        return "cleaned up expired browser sessions"
    return "used the browser"


def _browser_status_text(tool_name: str, args: dict, urls: dict, result_metadata: dict | None = None) -> str:
    args = args or {}
    result_metadata = result_metadata or {}
    target_id = str(args.get("target_id") or result_metadata.get("target_id") or "").strip()
    result_url = str(result_metadata.get("url") or "").strip()
    arg_url = str(args.get("url") or "").strip()
    if target_id and result_url:
        urls[target_id] = result_url
    url = arg_url or result_url or (urls.get(target_id) if target_id else "")
    if not url and tool_name == "list_browser_tabs":
        url = "open tabs"
    if not url:
        url = "current tab"
    return "🖥️ Computer use\nURL: " + _compact_status_value(url, 220) + "\nAction: " + _browser_action_label(tool_name, args)


def _command_status_text(args: dict, tool_name: str = "terminal") -> str:
    args = args or {}
    command = _compact_status_value(
        args.get("command")
        or args.get("input")
        or args.get("working_directory")
        or args.get("session_id")
        or args.get("job_id")
        or "",
        500,
    ) or "(terminal operation)"
    return f"⌨️ Terminal\n{tool_name.replace('_', ' ')}\n$ {command}"


async def _refresh_persistent_status(chat_id: int, thread_id: int | None) -> None:
    """Edit current statuses or re-anchor them when a newer message exists."""
    status_store = getattr(globals, "persistent_status_messages", {})
    status_key = (chat_id, normalize_message_thread_id(chat_id, thread_id))
    status_stack = status_store.get(status_key)
    if isinstance(status_stack, PersistentStatusStack):
        try:
            await status_stack.note_newer_message()
            await status_stack.bring_to_bottom()
        except Exception as exc:
            logging.debug("TELEGRAM STATUS: unable to refresh chat=%s: %s", chat_id, exc)


_heartbeat_last_evaluation = {}
_heartbeat_last_proactive = {}
_heartbeat_daily_proactive_counts = {}

_HEARTBEAT_HOOK_RE = re.compile(
    r"\b("
    r"follow up|circle back|later|tomorrow|next week|next month|remind|remember|"
    r"need to|should|decide|decision|plan|project|stuck|worried|stress|"
    r"frustrated|excited|appointment|meeting|deadline|waiting|promised|you said"
    r")\b|[?？]",
    re.IGNORECASE,
)

_HEARTBEAT_EXCLUDED_CONTEXT_RE = re.compile(
    r"\b(security|camera|reolink|motion alert|snapshot|news|headline|reuters|fox news)\b",
    re.IGNORECASE,
)


def _thought_process_title(part_index: int = None, part_count: int = None) -> str:
    if part_index is not None and part_count and part_count > 1:
        return f"🧠 {MODEL_NAME}'s Thought Process (Part {part_index}/{part_count})"
    return f"🧠 {MODEL_NAME}'s Thought Process"


def _rich_thought_html(title: str, thought_text: str) -> str:
    escaped_title = telegram_escape(title)
    escaped_text = telegram_escape(thought_text).replace("\n", "<br/>")
    return "\n".join([
        "<details>",
        f"<summary>{escaped_title}</summary>",
        f"<p><i>{escaped_text}</i></p>",
        "</details>",
    ])


def _fallback_thought_html(title: str, thought_text: str) -> str:
    return (
        f"<b>{telegram_escape(title)}</b> (Expand to read):\n"
        f"<blockquote expandable><i>{telegram_escape(thought_text)}</i></blockquote>"
    )


def _final_reasoning_summary_html(summary: str) -> str:
    """Render the completed reasoning summary as a collapsed Telegram block."""
    return _fallback_thought_html("💭 Final reasoning summary", summary)


async def send_model_thought_message(chat_id: int, thought_text: str, *, part_index: int = None, part_count: int = None, message_thread_id: int = None):
    """Send intentionally visible model thoughts as hidden Rich Message details with HTML fallback."""
    clean_thought = str(thought_text or "").strip()
    if not clean_thought:
        return []
    title = _thought_process_title(part_index, part_count)
    return await send_rich_html_or_split_html_message(
        globals.application_bot,
        chat_id,
        _rich_thought_html(title, clean_thought),
        fallback_html_text=_fallback_thought_html(title, clean_thought),
        message_thread_id=message_thread_id,
    )


async def _build_supported_document_content_text(document, caption: str = "") -> str:
    document_type = detect_supported_document_type(
        filename=document.file_name,
        mime_type=document.mime_type,
    )
    fallback_name = document.file_name or f"upload.{document_type or 'bin'}"
    logging.info(
        "📄 TELEGRAM DOC: received filename=%s mime=%s size=%s detected_type=%s caption=%s",
        document.file_name,
        document.mime_type,
        getattr(document, "file_size", None),
        document_type,
        bool(caption),
    )
    if not document_type:
        logging.info(
            "📄 TELEGRAM DOC: unsupported document filename=%s mime=%s falling_back_to_caption_or_placeholder",
            document.file_name,
            document.mime_type,
        )
        return caption or "[Non-text message]"

    doc_file = await document.get_file()
    file_bytes = await doc_file.download_as_bytearray()
    extraction = await convert_document_bytes(
        bytes(file_bytes),
        filename=fallback_name,
        mime_type=document.mime_type,
    )
    logging.info(
        "📄 TELEGRAM DOC: extraction result filename=%s success=%s status=%s errors=%s",
        fallback_name,
        extraction.get("success"),
        extraction.get("docling_status"),
        len(extraction.get("errors") or []),
    )
    extracted_preview = ""
    visual_analysis = []
    if extraction.get("success"):
        extracted_preview = build_extracted_text_preview(
            extraction,
            max_len=12000,
            question=caption,
        )
        visual_analysis = await analyze_pdf_visual_fallback(
            bytes(file_bytes),
            extraction,
            question=caption,
        )
    error = None if extraction.get("success") else "; ".join(extraction.get("errors") or []) or "Document extraction failed."
    if extracted_preview:
        logging.info(
            "📄 TELEGRAM DOC: extracted preview attached filename=%s preview_chars=%s",
            fallback_name,
            len(extracted_preview),
        )
    elif error:
        logging.warning(
            "⚠️ TELEGRAM DOC: using fallback note filename=%s note=%s",
            fallback_name,
            error,
        )
    return build_document_context_text(
        source_name=fallback_name,
        source_type=document_type,
        mime_type=document.mime_type,
        caption=caption,
        docling_status=extraction.get("docling_status"),
        extracted_preview=extracted_preview,
        error=error,
        page_count=extraction.get("page_count"),
        visual_analysis=visual_analysis,
    )

def is_user_allowed(update: Update) -> bool:
    """Checks whether the Telegram sender is an allowed human user."""
    user = update.effective_user
    if not user or getattr(user, "is_bot", False):
        return False

    if not ALLOWED_USER_IDS:
        return bool(ALLOW_UNRESTRICTED_TELEGRAM_ACCESS)
    return user.id in ALLOWED_USER_IDS


def validate_telegram_access_policy() -> None:
    """Logs the effective Telegram access posture at startup."""
    if ALLOWED_USER_IDS or ALLOWED_BOT_IDS:
        logging.info(
            "🔐 TELEGRAM ACCESS: allowlist enabled for %s user(s) and %s bot(s).",
            len(ALLOWED_USER_IDS),
            len(ALLOWED_BOT_IDS),
        )
        return

    if ALLOW_UNRESTRICTED_TELEGRAM_ACCESS:
        logging.warning(
            "⚠️ TELEGRAM ACCESS: unrestricted Telegram access is explicitly enabled. "
            "Anyone who can message this bot can use enabled tools."
        )
        return

    logging.critical(
        "🚫 TELEGRAM ACCESS: no allowed_user_ids or allowed_bot_ids are configured and unrestricted access is disabled. "
        "The bot will start but ignore all Telegram senders until config/users.json includes allowed IDs "
        "or ALLOW_UNRESTRICTED_TELEGRAM_ACCESS=true is set."
    )


async def validate_telegram_routing(application) -> bool:
    """Checks the configured Telegram group/topic routing and logs actionable warnings."""
    from emery.config import SECURITY_TOPIC_ID, ROUTINES_TOPIC_ID, CHAT_TOPIC_ID

    group_chat_id = TELEGRAM_GROUP_CHAT_ID
    topic_ids = {
        "security_topic_id": SECURITY_TOPIC_ID,
        "routines_topic_id": ROUTINES_TOPIC_ID,
        "chat_topic_id": CHAT_TOPIC_ID,
    }

    configured_topics = {
        name: topic_id
        for name, topic_id in topic_ids.items()
        if topic_id is not None
    }

    if group_chat_id is None:
        if configured_topics:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: topic IDs are configured (%s), but telegram.group_chat_id is missing. "
                "Security alerts and scheduled routines will fall back to in-memory chat state until the bot is restarted in a chat.",
                ", ".join(f"{name}={topic_id}" for name, topic_id in configured_topics.items()),
            )
        else:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: telegram.group_chat_id is not configured. "
                "Security alerts, routines, and heartbeat routing may be unstable until a chat is established."
            )
        return False

    try:
        chat = await application.bot.get_chat(group_chat_id)
        chat_title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(group_chat_id)
        chat_type = getattr(chat, "type", "unknown")
        is_forum = bool(getattr(chat, "is_forum", False))

        logging.info(
            "📡 TELEGRAM ROUTING: resolved group chat %s (type=%s, forum=%s)",
            chat_title,
            chat_type,
            is_forum,
        )

        if configured_topics and not is_forum:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: topic IDs are configured for chat_id=%s, but Telegram reports the chat is not a forum. "
                "Topic sends may fail until the group is converted to a forum or the topic IDs are removed.",
                group_chat_id,
            )

        if len(configured_topics) > 1:
            duplicate_ids = {}
            for name, topic_id in configured_topics.items():
                duplicate_ids.setdefault(topic_id, []).append(name)
            duplicate_ids = {topic_id: names for topic_id, names in duplicate_ids.items() if len(names) > 1}
            if duplicate_ids:
                logging.warning(
                    "⚠️ TELEGRAM ROUTING: some topic IDs are reused across multiple roles: %s",
                    ", ".join(f"{topic_id}={names}" for topic_id, names in duplicate_ids.items()),
                )

        if not configured_topics:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: telegram.group_chat_id is configured, but no topic IDs are set. "
                "That's fine for a non-forum group, but forum-specific delivery will use the chat root."
            )

        return True
    except Exception as e:
        logging.warning(
            "⚠️ TELEGRAM ROUTING: unable to verify configured group chat %s via Telegram API: %s",
            group_chat_id,
            e,
        )
        return False

# --- TELEGRAM HANDLERS ---
def _help_text() -> str:
    return "\n".join([
        "<b>Emery commands</b>",
        "",
        "<b>General</b>",
        "/help - Show this command list.",
        "/clear - Clear this chat/thread's active context and session approvals.",
        "/temporary &lt;on|off&gt; - Toggle an ephemeral raw-model conversation without tools or memory.",
        "/image [low|medium|high] [#inbatch count] &lt;prompt&gt; - Low (default): 512x512, 10 steps, max 10; Medium: 768x768, 12 steps, max 5; High: 1024x1024, 20 steps, max 2.",
        "/image ultra &lt;portrait|landscape&gt; &lt;prompt&gt; - One image, 30 steps, 1080x1920 portrait or 1920x1080 landscape.",
        "/image ultrabatch &lt;portrait|landscape&gt; &lt;prompt&gt; - 15 images, 30 steps, 1080x1920 portrait or 1920x1080 landscape.",
        "/image pause|resume|cancel - Pause and resume queued batches, or cancel image work in this chat/thread.",
        "/image-edit [low|medium|high] &lt;instructions&gt; - Edit the attached photo with one image, preserving its aspect ratio.",
        "/image-edit ultra &lt;instructions&gt; - Edit at 30 steps, matching the attached photo's aspect ratio.",
        "/notes - Show the current chat/thread scratchpad.",
        "/clear_notes - Clear the current chat/thread scratchpad.",
        "/wipe - Wipe your persistent memory and restore its baseline template.",
        "/bridge &lt;message&gt; - Send an authenticated request to the connected bot.",
        "/approve &lt;approval-id&gt; - Approve a pending command once.",
        "/deny &lt;approval-id&gt; - Deny a pending command.",
        "/skills help - Show skill command usage.",
        "/skills list|pending - List visible skills or pending changes.",
        "/skills search &lt;query&gt; - Search visible skills.",
        "/skills show &lt;id-or-name&gt; - Show a skill and its procedure.",
        "/skills status &lt;id-or-name&gt; - Show a skill's lifecycle status.",
        "/skills diff &lt;id&gt; - Review a pending skill change.",
        "/skills approve|reject &lt;id&gt; - Approve or reject a pending skill change.",
        "/skills archive &lt;id-or-name&gt; - Archive a visible skill.",
        "",
        "<b>Expert research</b>",
        "/expert &lt;topic&gt; - Start a foreground deep research session.",
        "/expert help - Show expert command help.",
        "/expert notes - Show the active research scratchpad.",
        "/expert list - Show archived expert sessions with inline actions.",
        "/expert status - Show the active expert session status for this chat/thread.",
        "/expert resume &lt;id&gt; - Load an archived expert session without auto-continuing research.",
        "/expert open &lt;id&gt; - Send the archived report for a session.",
        "/expert clear - Delete all archived expert reports.",
        "/expert cancel - Cancel the active expert session in this chat/thread.",
        "",
        "<b>While an expert report is complete</b>",
        "Use the inline buttons to continue researching, refine the report, close/archive, or cancel.",
        "Typed replies like \"move on\" or \"archive this\" close and archive the active expert session.",
        "",
        "<b>Debate mode</b>",
        "/debate &lt;topic&gt; - Start a four-role debate with Moderator, two named sides, and Clerk.",
        "/debate help - Show debate command help.",
        "/debate status - Show the active debate status for this chat/thread.",
        "/debate list - Show archived debates.",
        "/debate open &lt;id&gt; - Send an archived debate memo.",
        "/debate clear - Delete archived debates.",
        "/debate cancel - Cancel the active debate.",
        "",
        "<b>Natural language tools</b>",
        "You can also ask normally for reminders/routines, weather, web research, news, finance/econ data, images, voice replies, memory updates, smart-home actions, infrastructure checks, and recipe imports when those integrations are enabled.",
    ])


async def handle_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for /help command."""
    if not is_user_allowed(update):
        return
    await update.message.reply_text(_help_text(), parse_mode="HTML")


def _image_command_help(error: str | None = None) -> str:
    lines = [
        "<b>Image command</b>",
        "Usage: <code>/image [low|medium|high] [#inbatch count] &lt;description&gt;</code>",
        "Ultra: <code>/image ultra &lt;portrait|landscape&gt; &lt;description&gt;</code> (one image).",
        "Ultrabatch: <code>/image ultrabatch &lt;portrait|landscape&gt; &lt;description&gt;</code> (15 images).",
        "Controls: <code>/image pause</code>, <code>/image resume</code>, <code>/image cancel</code>.",
        "Pause stops the active image and continues the batch with unfinished images after resume. New batches can queue while paused.",
        "Omit the profile for Low. Omit the batch option to generate one image.",
        "",
        "<b>Profiles</b>",
        "Low: 512x512, 10 steps, up to 10 images.",
        "Medium: 768x768, 12 steps, up to 5 images.",
        "High: 1024x1024, 20 steps, up to 2 images.",
        "Ultra: portrait 1080x1920 or landscape 1920x1080, 30 steps, one image.",
        "Ultrabatch: portrait 1080x1920 or landscape 1920x1080, 30 steps, 15 images.",
        "",
        "Examples:",
        "<code>/image a red fox in a snowy forest</code>",
        "<code>/image medium a red fox in a snowy forest</code>",
        "<code>/image high #inbatch 2 a red fox in a snowy forest</code>",
        "<code>/image ultra portrait a red fox in a snowy forest</code>",
        "<code>/image ultrabatch portrait a red fox in a snowy forest</code>",
    ]
    if error:
        lines.insert(0, f"⚠️ {error}\n")
    return "\n".join(lines)


async def _reanchor_paused_image_status(chat_id: int, thread_id: int | None) -> None:
    state = get_active_image_generation(chat_id, thread_id)
    if state is not None and state.paused and state.pause_ready_event.is_set():
        await state.notifier.repost_at_bottom()


async def handle_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue one or more images directly without invoking Emery or writing chat history."""
    if not is_user_allowed(update):
        return

    args = list(context.args or [])
    if args and args[0].lower() in {"pause", "resume", "cancel"}:
        action = args[0].lower()
        if not update.message:
            return
        if len(args) != 1 or not update.effective_chat:
            await update.message.reply_text(_image_command_help("Use a control command by itself."), parse_mode="HTML")
            return
        chat_id = update.effective_chat.id
        thread_id = normalize_message_thread_id(chat_id, update.message.message_thread_id)
        if action == "pause":
            state = await pause_image_queue(context.bot, chat_id, thread_id)
            response = (
                "Image jobs are paused. Emery is available again; completed images are kept, and the active batch resumes with unfinished images after <code>/image resume</code>."
            )
        elif action == "resume":
            state, changed = await resume_image_queue(chat_id, thread_id)
            response = (
                "Image generation resumed; queued images will continue."
                if changed
                else "There are no paused image jobs in this chat/thread."
            )
        else:
            state, changed, queued = await cancel_image_queue(chat_id, thread_id)
            if not changed:
                response = "There are no active or queued image jobs in this chat/thread."
            else:
                response = f"Cancellation requested. Removed {queued} queued image batch(es); the active image is stopping."
        await update.message.reply_text(response, parse_mode="HTML")
        if state is not None and state.active and state.paused:
            await state.notifier.repost_at_bottom()
        return

    if not args or args[0].lower() == "help":
        await update.message.reply_text(_image_command_help(), parse_mode="HTML")
        return

    quality_profile = DIRECT_IMAGE_DEFAULT_PROFILE
    orientation = None
    batch_size = 1
    if args and args[0].lower() in {"low", "medium", "high", "ultra", "ultrabatch"}:
        quality_profile = args.pop(0).lower()

    if quality_profile in {"ultra", "ultrabatch"}:
        if (
            len(args) < 2
            or args[0].lower() not in {"portrait", "landscape"}
        ):
            await update.message.reply_text(
                _image_command_help(
                    "Ultra and ultrabatch require portrait or landscape, then a description."
                ),
                parse_mode="HTML",
            )
            return
        orientation = args.pop(0).lower()
        if quality_profile == "ultrabatch":
            batch_size = 15
    elif args and re.fullmatch(r"\d+", args[0]):
        batch_size = int(args.pop(0))

    batch_limit = image_profile_batch_limit(quality_profile, IMAGE_MAX_BATCH_SIZE)
    if quality_profile not in {"ultra", "ultrabatch"} and args and args[0].lower() == "#inbatch":
        args.pop(0)
        if not args or not re.fullmatch(r"\d+", args[0]):
            await update.message.reply_text(
                _image_command_help("#inbatch must be followed by an image count."),
                parse_mode="HTML",
            )
            return
        batch_size = int(args.pop(0))
    elif quality_profile not in {"ultra", "ultrabatch"} and args and re.fullmatch(r"#inbatch=(\d+)", args[0], flags=re.IGNORECASE):
        batch_size = int(re.fullmatch(r"#inbatch=(\d+)", args.pop(0), flags=re.IGNORECASE).group(1))
    elif quality_profile != DIRECT_IMAGE_DEFAULT_PROFILE and quality_profile not in {"ultra", "ultrabatch"} and args and re.fullmatch(r"\d+", args[0]):
        batch_size = int(args.pop(0))
    elif quality_profile not in {"ultra", "ultrabatch"} and args and args[0].lower().startswith("#inbatch"):
        await update.message.reply_text(
            _image_command_help("Invalid batch option."),
            parse_mode="HTML",
        )
        return

    if not 1 <= batch_size <= batch_limit:
        await update.message.reply_text(
            _image_command_help(
                f"The {quality_profile} profile allows 1-{batch_limit} images."
            ),
            parse_mode="HTML",
        )
        return

    prompt = " ".join(args).strip()
    if not prompt:
        await update.message.reply_text(
            _image_command_help("A description is required."),
            parse_mode="HTML",
        )
        return

    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(
        chat_id,
        update.message.message_thread_id if update.message else None,
    )
    try:
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action="upload_photo",
            message_thread_id=thread_id,
        )
        queue_image_generation(
            prompt,
            chat_id,
            thread_id,
            batch_size=batch_size,
            quality_profile=quality_profile,
            orientation=orientation,
            bot=context.bot,
            reply_to_message_id=update.message.message_id,
        )
        profile = get_image_profile(quality_profile, orientation=orientation)
        count_text = f"{batch_size} images" if batch_size != 1 else "1 image"
        await update.message.reply_text(
            f"Image generation queued: {count_text}, {profile.name} profile "
            f"({profile.width}x{profile.height}, {profile.steps} steps)."
        )
        logging.info(
            "🖼️ DIRECT IMAGE: queued for chat_id=%s profile=%s count=%d without Emery context.",
            chat_id,
            quality_profile,
            batch_size,
        )
    except Exception as exc:
        logging.error("❌ DIRECT IMAGE: generation failed for chat_id=%s: %s", chat_id, exc, exc_info=True)
        await update.message.reply_text(f"Direct image generation failed: {safe_preview(str(exc), max_len=500)}")


async def handle_image_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit a Telegram photo with /image-edit instructions in its caption."""
    if not is_user_allowed(update) or not update.message:
        return

    message = update.message
    command_text = message.caption or ""
    match = re.match(r"^\s*/image-edit(?:@\w+)?(?:\s+([\s\S]*))?\s*$", command_text, flags=re.IGNORECASE)
    if not match:
        return
    prompt = (match.group(1) or "").strip()
    if prompt.lower() == "help":
        await message.reply_text(
            "Attach one photo and use /image-edit [low|medium|high] edit instructions. "
            "For Ultra, use /image-edit ultra followed by the edit instructions. "
            "Edits match the original aspect ratio; batching is unavailable."
        )
        return
    if not message.photo:
        await message.reply_text("Attach a photo to the same message as /image-edit and its instructions.")
        return
    if not prompt:
        await message.reply_text("Add edit instructions after /image-edit.")
        return

    args = prompt.split()
    quality_profile = DIRECT_IMAGE_DEFAULT_PROFILE
    orientation = None
    if args and args[0].lower() in {"low", "medium", "high", "ultra"}:
        quality_profile = args.pop(0).lower()
    elif args and args[0].lower() == "ultrabatch":
        await message.reply_text("Image editing makes one image at a time; ultrabatch is not available.")
        return
    if quality_profile == "ultra":
        orientation = (
            "landscape"
            if message.photo[-1].width >= message.photo[-1].height
            else "portrait"
        )
    prompt = " ".join(args).strip()
    if not prompt:
        await message.reply_text("Add edit instructions after the profile, if supplied.")
        return

    try:
        photo_file = await message.photo[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())
        chat_id = update.effective_chat.id
        thread_id = normalize_message_thread_id(
            chat_id,
            message.message_thread_id,
        )
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action="upload_photo",
            message_thread_id=thread_id,
        )
        queue_image_generation(
            prompt,
            chat_id,
            thread_id,
            batch_size=1,
            quality_profile=quality_profile,
            orientation=orientation,
            input_image_bytes=photo_bytes,
            bot=context.bot,
            reply_to_message_id=message.message_id,
            caption_prefix="Edited image\n",
        )
        profile = get_image_profile(quality_profile, orientation=orientation)
        await message.reply_text(
            f"Image edit queued with Qwen Image ({profile.name}, {profile.steps} steps)."
        )
        logging.info(
            "🖼️ IMAGE EDIT: queued for chat_id=%s source_message_id=%s.",
            chat_id,
            message.message_id,
        )
    except Exception as exc:
        logging.error("❌ IMAGE EDIT: request failed for chat_id=%s: %s", update.effective_chat.id, exc, exc_info=True)
        await message.reply_text(f"Image edit could not be queued: {safe_preview(str(exc), max_len=400)}")


async def handle_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for /clear command."""
    if not is_user_allowed(update):
        return
    chat_id = update.effective_chat.id
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(
        normalize_message_thread_id(chat_id, update.message.message_thread_id if update.message else None)
    )
    globals.current_user_id.set(getattr(update.effective_user, "id", None))
    if chat_id in globals.chat_histories:
        globals.chat_histories[chat_id].clear()
    clear_session_context_cache(chat_id=chat_id)
    from emery.command_approval import clear_session_approvals
    cleared_approvals = clear_session_approvals(
        chat_id,
        globals.CURRENT_THREAD_ID.get(),
        globals.current_user_id.get(),
    )
    suffix = f" Cleared {cleared_approvals} session approval(s)." if cleared_approvals else ""
    await update.message.reply_text(f"Context cleared.{suffix}")


async def handle_temporary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a raw, ephemeral conversation for the current chat/thread."""
    if not is_user_allowed(update):
        return
    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(
        chat_id,
        update.message.message_thread_id if update.message else None,
    )
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(thread_id)
    globals.current_user_id.set(getattr(update.effective_user, "id", None))

    argument = " ".join(context.args or []).strip().casefold()
    if argument not in {"on", "off"}:
        state = "on" if is_temporary_mode(chat_id, thread_id) else "off"
        await update.message.reply_text(
            f"Temporary mode is {state}. Use /temporary on or /temporary off."
        )
        return

    enabled = argument == "on"
    set_temporary_mode(chat_id, thread_id, enabled)
    clear_session_context_cache(chat_id=chat_id, thread_id=thread_id)
    if enabled:
        await update.message.reply_text(
            "🕶️ Temporary mode on — raw model only; no prompt, tools, or memory."
        )
    else:
        await update.message.reply_text(
            "🕶️ Temporary mode off — normal prompt, tools, memory, and prior context restored."
        )


async def handle_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the persistent scratchpad for the current chat/thread."""
    if not is_user_allowed(update):
        return
    chat_id = update.effective_chat.id
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(
        normalize_message_thread_id(chat_id, update.message.message_thread_id if update.message else None)
    )
    scratchpad = await read_scratchpad()
    await send_rich_or_split_html_message(
        context.bot,
        chat_id,
        f"# Scratchpad\n\n{scratchpad}",
        fallback_html_text=emery_format(f"# Scratchpad\n\n{scratchpad}"),
        message_thread_id=globals.CURRENT_THREAD_ID.get(),
    )


async def handle_clear_notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the persistent scratchpad for the current chat/thread."""
    if not is_user_allowed(update):
        return
    chat_id = update.effective_chat.id
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(
        normalize_message_thread_id(chat_id, update.message.message_thread_id if update.message else None)
    )
    await update.message.reply_text(await clear_scratchpad())


async def _clear_completed_turn_scratchpad(temporary_response_mode: bool) -> None:
    """Discard working notes once a model turn has finished delivering."""
    if temporary_response_mode:
        return
    try:
        await clear_scratchpad()
    except Exception as exc:
        logging.warning("SCRATCHPAD: unable to clear notes after completed turn: %s", exc)


async def handle_wipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for /wipe command."""
    if not is_user_allowed(update):
        return
        
    globals.current_user_id.set(update.effective_user.id)
    if wipe_memory(update.effective_user.id):
        await update.message.reply_text("🧠 Memory wiped successfully and re-initialized to baseline template.")
    else:
        await update.message.reply_text("❌ Failed to wipe memory due to a filesystem error.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    if not is_user_allowed(update):
        return
        
    globals.current_user_id.set(update.effective_user.id)
    chat_id = update.effective_chat.id
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(
        normalize_message_thread_id(
            chat_id,
            update.message.message_thread_id if update.message else None,
        )
    )

    # Refresh the existing status messages in place. Telegram cannot move
    # them below this incoming message without the intrusive delete/send
    # animation, so their message IDs remain stable for the turn.
    await _refresh_persistent_status(chat_id, globals.CURRENT_THREAD_ID.get())
    
    # Dynamically associate user chat ID with any pending jobs (like default briefings)
    from emery.scheduler import update_jobs_with_chat_id
    update_jobs_with_chat_id(chat_id)
    
    if chat_id not in globals.chat_histories: 
         globals.chat_histories[chat_id] = deque()
    
    # Clear any stale custom reply targets for this turn
    globals.chat_reply_targets.pop(chat_id, None)

    is_input_voice = False
    model_to_use = MODEL_ID
    image_artifact_id = None
    edit_image_artifact_id = None
    
    now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")
    sender_name = update.effective_user.first_name or "User"

    if update.message.voice:
        is_input_voice = True
        v_file = await update.message.voice.get_file()
        transcription = await transcribe_audio(await v_file.download_as_bytearray())
        if not transcription: 
            return
        content_text = transcription
    elif update.message.photo:
        p_file = await update.message.photo[-1].get_file()
        photo_bytes = await p_file.download_as_bytearray()
        compressed_bytes = compress_image_bytes(photo_bytes)
        b64 = base64.b64encode(compressed_bytes).decode('utf-8')
        caption = update.message.caption or ""
        
        await update.message.reply_chat_action("typing")
        from emery.media import store_artifact
        try:
            edit_image_artifact_id = store_artifact(
                compress_image_bytes(photo_bytes, max_dim=1920, quality=90),
                mime_type="image/jpeg",
                label="editable user image",
            )
        except ValueError:
            logging.warning("IMAGE EDIT: source photo exceeded artifact limit; using vision-sized copy")
            edit_image_artifact_id = store_artifact(
                bytes(compressed_bytes),
                mime_type="image/jpeg",
                label="editable user image",
            )
        if MAIN_MODEL_VISION:
            image_artifact_id = store_artifact(
                bytes(compressed_bytes),
                mime_type="image/jpeg",
                label="user image",
            )
        description = ""
        if not MAIN_MODEL_VISION:
            description = await get_image_description(b64, caption)

        content_text = "sent an image."
        if caption:
            content_text += f" Caption: {caption}"
        if description:
            content_text += f"\nImage Description: {description}"
    elif update.message.sticker:
        sticker = update.message.sticker
        emoji = sticker.emoji or ""
        file_id = sticker.file_id
        set_name = sticker.set_name or "Unknown"
        
        if emoji:
            globals.learned_stickers[emoji] = file_id
            
        content_text = f"sent a sticker: {emoji} (File ID: {file_id}, Set: {set_name})"
    elif update.message.animation:
        anim = update.message.animation
        file_id = anim.file_id
        content_text = f"sent a GIF / Animation (File ID: {file_id})"
    elif update.message.document and update.message.document.mime_type == "video/mp4":
        doc = update.message.document
        file_id = doc.file_id
        content_text = f"sent a GIF / Animation (File ID: {file_id})"
    elif update.message.document:
        content_text = await _build_supported_document_content_text(
            update.message.document,
            update.message.caption or "",
        )
    else:
        content_text = update.message.text or "[Non-text message]"

    # Expert sessions own their chat/thread while waiting for user direction.
    # Route typed answers and close/archive intents there before normal chat handling.
    if update.message.text:
        from emery.debate import handle_debate_message
        if await handle_debate_message(update, context, content_text):
            return
        from emery.expert import handle_expert_message
        if await handle_expert_message(update, context, content_text):
            return
        
    content = f"[{now_str}] {sender_name}: {content_text}"
        
    # Thread Reply Context Builder
    reply_to = update.message.reply_to_message
    reply_info = ""
    if reply_to:
        reply_to_id = reply_to.message_id
        replied_text = ""
        for msg in globals.chat_histories[chat_id]:
            if msg.get("message_id") == reply_to_id or (isinstance(msg.get("message_ids"), list) and reply_to_id in msg["message_ids"]):
                replied_text = msg.get("content", "")
                replied_text = clean_thinking_tags(replied_text)
                break
        if not replied_text:
            replied_text = reply_to.text or "[Non-text message]"
        preview = safe_preview(replied_text, max_len=80)
        reply_info = f" (Replying to message ID {reply_to_id}: '{preview}')"

    temporary_response_mode = is_temporary_mode(chat_id, globals.CURRENT_THREAD_ID.get())
    if temporary_response_mode:
        session_context = None
        turn_context = None
        globals.CURRENT_SESSION_CONTEXT.set(None)
        globals.CURRENT_TURN_CONTEXT.set(None)
    else:
        session_context = await get_session_context(
            chat_id=chat_id,
            thread_id=globals.CURRENT_THREAD_ID.get(),
            user_id=update.effective_user.id,
        )
        turn_context = await get_turn_context(
            content_text,
            update.effective_user.id,
            session=session_context,
        )
        set_current_context(session_context, turn_context)
    # Runtime context is supplied separately to the engine.  Keep history as
    # the actual conversation so old histories remain readable and compact.
    history_content = f"{content}{reply_info}"

    logging.info(f"💬 USER (chat {chat_id}): {sender_name} -> {safe_preview(content_text, max_len=120)}{reply_info}")
    user_history_entry = {
        "role": "user", 
        "content": history_content,
        "message_id": update.message.message_id,
        "user_id": update.effective_user.id,
        "sender_name": sender_name,
        "reply_to_message_id": reply_to.message_id if reply_to else None,
        "message_thread_id": update.message.message_thread_id if update.message else None,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    if update.message.photo and edit_image_artifact_id:
        user_history_entry["media_attachments"] = [{
            "artifact_id": image_artifact_id or edit_image_artifact_id,
            "edit_artifact_id": edit_image_artifact_id,
            "kind": "user_image",
        }]
    globals.chat_histories[chat_id].append(user_history_entry)
    
    # Check if this chat is a group chat
    is_group = (chat_id < 0)
    
    # By default, we reply in DMs (positive chat_id)
    should_reply = not is_group
    
    # If it is a group chat, we only reply if:
    # 1. The bot is mentioned (e.g. @EmeryBot)
    # 2. It is a reply to one of the bot's own messages
    # 3. The message starts with a slash command
    if is_group:
        bot_username = (await context.bot.get_me()).username.lower()
        
        # Check mentions in text or caption
        text_lower = ""
        if update.message.text:
            text_lower = update.message.text.lower()
        elif update.message.caption:
            text_lower = update.message.caption.lower()
            
        is_mentioned = f"@{bot_username}" in text_lower
        
        is_reply_to_bot = False
        if update.message.reply_to_message:
            is_reply_to_bot = (update.message.reply_to_message.from_user.id == context.bot.id)
            
        is_command = text_lower.startswith("/")
        
        if is_mentioned or is_reply_to_bot or is_command:
            should_reply = True

    if not should_reply:
        logging.debug(f"🤫 SILENT LISTEN: Recorded group message from {sender_name} (chat {chat_id}) for context, but not replying.")
        return

    if await defer_active_chat_message(update, context, user_history_entry):
        return

    turn_key = (
        chat_id,
        normalize_message_thread_id(
            chat_id,
            update.message.message_thread_id if update.message else None,
        ),
    )
    active_turn = globals.active_turns.get(turn_key)
    if active_turn and active_turn.accepting:
        if len(active_turn.pending_messages) >= active_turn.max_pending:
            await update.message.reply_text(
                "I’m already adjusting the current response; please wait a moment before sending another change."
            )
            return

        active_turn.pending_messages.append(user_history_entry)
        active_turn.steer_event.set()
        if active_turn.notify:
            await active_turn.notify({
                "type": "steering_queued",
                "text": "I got that — I’ll adjust course.",
                "source": "application",
            })
        logging.info("🧭 STEERING: queued message for chat=%s thread=%s", chat_id, turn_key[1])
        return

    # Start the main model response as soon as this message is received.
    async def response_worker():
        try:
            await run_engine_for_chat(update, context, model_to_use, is_input_voice)
        finally:
            turn_key = (
                chat_id,
                normalize_message_thread_id(
                    chat_id,
                    update.message.message_thread_id if update.message else None,
                ),
            )
            active_turn = globals.active_turns.get(turn_key)
            if active_turn is not None:
                active_turn.accepting = False
                globals.active_turns.pop(turn_key, None)
            task = asyncio.current_task()
            if globals.chat_response_tasks.get(chat_id) is task:
                globals.chat_response_tasks.pop(chat_id, None)

    globals.chat_response_tasks[chat_id] = asyncio.create_task(response_worker())

async def _deliver_pending_media(chat_id: int, reply_to_message_id: int | None = None) -> list:
    """Deliver model-approved media after the final text has been produced."""
    from emery.media import get_artifact, take_outbound_media

    pending = take_outbound_media()
    if not pending:
        return []

    thread_id = normalize_message_thread_id(chat_id, globals.CURRENT_THREAD_ID.get())
    sent = []
    for index, item in enumerate(pending):
        artifact = get_artifact(item.get("artifact_id"))
        if not artifact:
            logging.warning("⚠️ TELEGRAM: queued media artifact expired before delivery.")
            continue
        reply_params = None
        if index == 0 and reply_to_message_id:
            reply_params = ReplyParameters(
                message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        try:
            sent_msg = await globals.application_bot.send_photo(
                chat_id=chat_id,
                photo=artifact["bytes"],
                caption=str(item.get("caption") or "").strip()[:1024] or None,
                reply_parameters=reply_params,
                message_thread_id=thread_id,
            )
            if sent_msg:
                sent.append(sent_msg)
        except Exception as exc:
            logging.error("❌ TELEGRAM: Failed to deliver queued research image: %s", exc, exc_info=True)
    return sent


async def run_engine_for_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, model_to_use: str, is_input_voice: bool) -> None:
    chat_id = update.effective_chat.id
    globals.chat_histories.setdefault(chat_id, deque())

    turn_key = (
        chat_id,
        normalize_message_thread_id(
            chat_id,
            update.message.message_thread_id if update.message else None,
        ),
    )
    steering_state = None
    if ENABLE_LIVE_STEERING:
        # Register before the first await so an immediate follow-up can steer
        # this turn instead of starting a second model request.
        steering_state = globals.ActiveTurnState(
            chat_id=chat_id,
            thread_id=turn_key[1],
            max_pending=LIVE_STEERING_MAX_PENDING,
        )
        globals.active_turns[turn_key] = steering_state

    from emery.media import begin_media_turn, clear_media_turn
    begin_media_turn()
    
    # Determine final reply target from globals
    reply_target_id = globals.chat_reply_targets.pop(chat_id, None)
    current_thread_id = normalize_message_thread_id(chat_id, globals.CURRENT_THREAD_ID.get())
    temporary_response_mode = is_temporary_mode(chat_id, current_thread_id)

    # --- TYPING INDICATOR LOOP ---
    typing_stop = asyncio.Event()

    async def keep_typing():
        while not typing_stop.is_set():
            try:
                await globals.application_bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logging.debug(f"Typing action failed: {e}")
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    status_stack = None
    final_reasoning_summary = False
    status_key = None
    status_store = None
    if ENABLE_LIVE_PROGRESS:
        status_key = (
            chat_id,
            normalize_message_thread_id(chat_id, globals.CURRENT_THREAD_ID.get()),
        )
        status_store = getattr(globals, "persistent_status_messages", None)
        if status_store is None:
            status_store = {}
            setattr(globals, "persistent_status_messages", status_store)
        status_stack = status_store.get(status_key)
        if isinstance(status_stack, PersistentStatusStack) and status_stack.disabled:
            # A Telegram formatting or transport error must not permanently
            # suppress progress for every later turn in this chat.
            try:
                await status_stack.close()
            except Exception:
                logging.debug("TELEGRAM STATUS: unable to retire disabled status stack", exc_info=True)
            status_stack = None
        if not isinstance(status_stack, PersistentStatusStack):
            status_stack = PersistentStatusStack(
                globals.application_bot,
                chat_id,
                message_thread_id=status_key[1],
            )
            status_store[status_key] = status_stack

        controllers = getattr(globals, "persistent_status_controllers", None)
        if controllers is None:
            controllers = {}
            setattr(globals, "persistent_status_controllers", controllers)
        controllers[status_key] = status_stack
        await status_stack.ensure_visible()

    def friendly_tool_name(event: dict) -> str:
        explicit_name = (
            event.get("friendly_name")
            or event.get("display_name")
            or event.get("tool_label")
            or event.get("label")
        )
        if explicit_name:
            return str(explicit_name).strip()

        raw_name = event.get("name") or event.get("tool_name") or event.get("fn")
        if raw_name:
            raw_name = str(raw_name).strip()
            return TOOL_DISPLAY_NAMES.get(
                raw_name,
                raw_name.replace("_", " ").replace("-", " ").strip().lower(),
            )
        return "a tool"

    async def update_status_slot(
        slot: str,
        text: str | None,
        *,
        force: bool = True,
        mode: str | None = None,
        rendered_html: str | None = None,
    ) -> None:
        if status_stack is None:
            return
        await status_stack.set_slot(
            slot,
            text,
            force=force,
            mode=mode,
            rendered_html=rendered_html,
        )

    async def handle_engine_event(event: dict) -> None:
        nonlocal final_reasoning_summary
        if status_stack is None:
            return

        event_type = event.get("type")
        if event_type == "prefill_started":
            await update_status_slot("reasoning", PREFILL_FALLBACK_STATUS)
        elif event_type == "prefill_progress":
            try:
                percent = max(0, min(100, int(event.get("percent"))))
            except (TypeError, ValueError):
                percent = None
            if percent is not None:
                await update_status_slot("reasoning", f"Preparing response: {percent}%")
        elif event_type == "reasoning_summary":
            summary = str(event.get("text") or event.get("summary") or "").strip()
            if summary:
                if event.get("full_turn"):
                    final_reasoning_summary = True
                    # The final collapsed summary keeps only the reasoning
                    # and tool-call timeline. Remove the live tool and
                    # browser/terminal state messages before showing it.
                    await status_stack.close(preserve_slots={"reasoning"})
                await update_status_slot(
                    "reasoning",
                    summary,
                    rendered_html=(
                        _final_reasoning_summary_html(summary)
                        if event.get("full_turn")
                        else None
                    ),
                )
        elif event_type == "preamble":
            preamble = str(event.get("text") or "").strip()
            if preamble:
                await update_status_slot("reasoning", preamble, force=False)
        elif event_type == "tool_started":
            # Engine status formatters historically returned HTML-escaped
            # fragments because they were sent directly as HTML. The fixed
            # status messages escape exactly once at delivery time, so decode
            # those fragments before storing them.
            status = html.unescape(str(event.get("text") or "")).strip()
            name = html.unescape(friendly_tool_name(event))
            explicit_name = html.unescape(str(
                event.get("friendly_name")
                or event.get("display_name")
                or event.get("tool_label")
                or event.get("label")
                or ""
            )).strip()
            if status and explicit_name and explicit_name == status:
                tool_text = f"🔧 {status}"
            elif status:
                tool_text = f"🔧 {name}\n{status}"
            else:
                tool_text = f"🔧 Using {name}…"
            tool_name = str(event.get("tool_name") or event.get("name") or "").strip()
            tool_args = event.get("args") if isinstance(event.get("args"), dict) else {}
            status_mode = "terminal" if tool_name in COMMAND_STATUS_TOOLS else "browser" if tool_name in BROWSER_STATUS_TOOLS else None
            await update_status_slot("tool", tool_text, mode=status_mode)
            if tool_name in COMMAND_STATUS_TOOLS:
                await update_status_slot("command", _command_status_text(tool_args, tool_name), mode="terminal")
            elif tool_name in BROWSER_STATUS_TOOLS:
                await update_status_slot(
                    "browser",
                    _browser_status_text(
                        tool_name,
                        tool_args,
                        status_stack.browser_urls,
                    ),
                )
        elif event_type == "tool_finished":
            tool_name = str(event.get("tool_name") or event.get("name") or "").strip()
            if tool_name in BROWSER_STATUS_TOOLS:
                tool_args = event.get("args") if isinstance(event.get("args"), dict) else {}
                await update_status_slot(
                    "browser",
                    _browser_status_text(
                        tool_name,
                        tool_args,
                        status_stack.browser_urls,
                        event.get("result_metadata") if isinstance(event.get("result_metadata"), dict) else {},
                    ),
                )
            # Keep the last tool call visible in slot two. Tool output may
            # have posted a normal Telegram message, so move the fixed block
            # back below it after the tool completes.
            await status_stack.bring_to_bottom()
        elif event_type in {"steering_queued", "steering_applied", "steering_deferred"}:
            await update_status_slot("reasoning", str(event.get("text") or "").strip())

    async def refresh_live_progress() -> None:
        """Keep visible live-status messages below newly received user text."""
        if status_stack is not None:
            await status_stack.bring_to_bottom()

    async def clear_completed_status(
        *,
        reanchor: bool = True,
    ) -> None:
        """Update the persistent reasoning message without adding status messages."""
        if status_stack is None:
            return
        try:
            await status_stack.clear_slots(
                "approval",
            )
            if reanchor:
                await status_stack.bring_to_bottom()
        except Exception as exc:
            logging.warning("TELEGRAM STATUS: unable to finalize status stack: %s", exc)

    async def close_completed_status(*, preserve_final_reasoning: bool = False) -> None:
        """Delete transient statuses while optionally keeping the final summary."""
        if status_stack is None:
            return
        try:
            await status_stack.close(
                preserve_slots={"reasoning"}
                if preserve_final_reasoning and final_reasoning_summary
                else set(),
            )
        except Exception as exc:
            logging.warning("TELEGRAM STATUS: unable to delete completed status stack: %s", exc)
        finally:
            if status_store is not None and status_key is not None:
                if status_store.get(status_key) is status_stack:
                    status_store.pop(status_key, None)

    if steering_state is not None:
        steering_state.notify = handle_engine_event if status_stack else None
        steering_state.refresh_progress = refresh_live_progress if status_stack else None

    try:
        from emery.engine import emery_engine
        history_buffer = globals.chat_histories[chat_id]
        if temporary_response_mode:
            history_buffer = temporary_history(history_buffer, chat_id, current_thread_id)
        response_text, voice_sent_via_tool = await emery_engine(
            history_buffer,
            model_to_use=model_to_use,
            allow_tools=not temporary_response_mode,
            on_event=handle_engine_event if status_stack else None,
            steering_state=steering_state,
            session_context=(globals.CURRENT_SESSION_CONTEXT.get().prompt
                             if globals.CURRENT_SESSION_CONTEXT.get() else None),
            turn_context=(globals.CURRENT_TURN_CONTEXT.get().prompt
                          if globals.CURRENT_TURN_CONTEXT.get() else None),
            raw_mode=temporary_response_mode,
        )
    except Exception as e:
        logging.error(f"Error running engine in debounce worker: {e}", exc_info=True)
        response_text = "EMERYCHAT engine failure."
        voice_sent_via_tool = False
    finally:
        if steering_state is not None:
            steering_state.accepting = False
            if globals.active_turns.get(turn_key) is steering_state:
                globals.active_turns.pop(turn_key, None)
        typing_stop.set()
        await typing_task
        controllers = getattr(globals, "persistent_status_controllers", None)
        if controllers is not None and status_key is not None:
            if controllers.get(status_key) is status_stack:
                controllers.pop(status_key, None)

    # --- THINKING SPLITTER LOGIC ---
    clean_response = clean_thinking_tags(response_text).strip()

    # --- SILENT HANDSHAKE DETECTION ---
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.debug("🤫 HANDSHAKE: Suppressed text reply (silent check)")
        try:
            await _deliver_pending_media(chat_id, reply_target_id)
        finally:
            await clear_completed_status(
                reanchor=False,
            )
            await close_completed_status()
        globals.chat_histories[chat_id].append({
            "role": "assistant",
            "content": response_text,
            "message_thread_id": globals.CURRENT_THREAD_ID.get(),
            "timestamp": datetime.now(USER_TIMEZONE)
        })
        await _clear_completed_turn_scratchpad(temporary_response_mode)
        clear_media_turn()
        await _reanchor_paused_image_status(chat_id, current_thread_id)
        return

    temporary_banner = "🕶️ Temporary mode — no long-term memory."
    delivered_response = (
        f"{temporary_banner}\n\n{clean_response}"
        if temporary_response_mode and clean_response
        else clean_response
    )

    # Raw model thinking is intentionally not emitted as separate Telegram
    # messages. The live status stack already contains the concise reasoning
    # summary, so the completed turn contains only that summary and the final
    # response.

    sent_msgs = []
    media_msgs = []
    # Clear any approval overlay before the final answer. The live status
    # messages remain visible through the final answer; the finalized
    # reasoning message is preserved as a collapsed summary.
    await clear_completed_status(
        reanchor=True,
    )
    try:
        # --- SINGLE FINAL REPLY DISPATCHER ---
        if is_input_voice and not voice_sent_via_tool:
            await globals.application_bot.send_chat_action(chat_id=chat_id, action="record_voice")
            from emery.tools import get_voice_audio
            v_out = await get_voice_audio(clean_response)
            if v_out:
                reply_params = ReplyParameters(message_id=reply_target_id, allow_sending_without_reply=True) if reply_target_id else None
                sent_msg = await globals.application_bot.send_voice(
                    chat_id=chat_id,
                    voice=v_out,
                    caption=temporary_banner if temporary_response_mode else "Voice message",
                    reply_parameters=reply_params,
                    message_thread_id=globals.CURRENT_THREAD_ID.get()
                )
                sent_msgs = [sent_msg] if sent_msg else []
            else:
                sent_msgs = await send_model_text_message_as_reply(chat_id, delivered_response, reply_to_message_id=reply_target_id, message_thread_id=globals.CURRENT_THREAD_ID.get())
        else:
            if delivered_response:
                sent_msgs = await send_model_text_message_as_reply(chat_id, delivered_response, reply_to_message_id=reply_target_id, message_thread_id=globals.CURRENT_THREAD_ID.get())
            elif not voice_sent_via_tool:
                logging.error("❌ TELEGRAM: Model returned no final response after reasoning budget enforcement.")
                sent_msgs = await send_model_text_message_as_reply(
                    chat_id,
                    f"{temporary_banner}\n\nI reached the reasoning limit before producing a final answer. Please resend your request."
                    if temporary_response_mode
                    else "I reached the reasoning limit before producing a final answer. Please resend your request.",
                    reply_to_message_id=reply_target_id,
                    message_thread_id=globals.CURRENT_THREAD_ID.get(),
                )

        media_msgs = await _deliver_pending_media(chat_id, reply_target_id)
        await close_completed_status(preserve_final_reasoning=True)
    except Exception:
        raise

    # Save the assistant text to history
    assistant_entry = {
        "role": "assistant", 
        "content": response_text,
        "message_thread_id": globals.CURRENT_THREAD_ID.get(),
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    if sent_msgs:
        assistant_entry["message_ids"] = [m.message_id for m in sent_msgs]
        assistant_entry["message_id"] = sent_msgs[-1].message_id
    if media_msgs:
        assistant_entry["media_message_ids"] = [m.message_id for m in media_msgs if getattr(m, "message_id", None)]
    globals.chat_histories[chat_id].append(assistant_entry)
    await _clear_completed_turn_scratchpad(temporary_response_mode)
    clear_media_turn()

    # Trigger background topic summarization
    from emery.memory import summarize_topics_background
    last_user_id = None
    for msg in reversed(globals.chat_histories[chat_id]):
        if msg.get("role") == "user" and msg.get("user_id"):
            last_user_id = msg.get("user_id")
            break
    if not temporary_response_mode:
        asyncio.create_task(summarize_topics_background(chat_id, last_user_id))
    await _reanchor_paused_image_status(chat_id, current_thread_id)

async def send_safe_large_message(update: Update, text: str, reply_to_message_id: int = None):
    """
    Splits extremely long final responses at natural line breaks 
    to prevent Telegram's 4096 character limit crash.
    """
    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(
        chat_id,
        update.message.message_thread_id if update.message else None,
    )
    return await send_split_html_message(
        globals.application_bot,
        chat_id,
        text,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=thread_id,
    )



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs network drops and timeouts cleanly instead of crashing the thread."""
    if isinstance(context.error, TimedOut):
        logging.warning("⚠️ TELEGRAM: API timed out temporarily due to load. The message will retry.")
    else:
        logging.error(f"❌ TELEGRAM: Unhandled API exception: {context.error}", exc_info=True)

# --- REACTION AND HEARTBEAT FUNCTIONALITY ---

async def send_safe_large_message_as_reply(chat_id: int, text: str, reply_to_message_id: int = None, message_thread_id: int = None):
    """Sends a safe split message directly to a chat, replying to a specific message ID."""
    return await send_split_html_message(
        globals.application_bot,
        chat_id,
        text,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )


async def send_model_text_message_as_reply(chat_id: int, markdown_text: str, reply_to_message_id: int = None, message_thread_id: int = None):
    """Sends model-authored assistant text using rich Markdown with legacy HTML fallback."""
    return await send_rich_or_split_html_message(
        globals.application_bot,
        chat_id,
        markdown_text,
        fallback_html_text=emery_format(markdown_text),
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )

async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for message reaction changes (MessageReactionUpdated)."""
    if not is_user_allowed(update):
        return
        
    reaction_update = update.message_reaction
    if not reaction_update:
        return
        
    chat_id = reaction_update.chat.id
    message_id = reaction_update.message_id
    user = reaction_update.user
    
    is_bot = (user.id == context.bot.id) if user else False
    actor_key = "assistant" if is_bot else "user"
    
    emojis = []
    for r in reaction_update.new_reaction:
        if r.type == "emoji":
            emojis.append(r.emoji)
        elif r.type == "custom_emoji":
            emojis.append("✨") # Use sparkle emoji as placeholder for custom emojis
            
    if chat_id not in globals.chat_histories:
        globals.chat_histories[chat_id] = deque()
        
    found_msg = None
    for msg in globals.chat_histories[chat_id]:
        if msg.get("message_id") == message_id or (isinstance(msg.get("message_ids"), list) and message_id in msg["message_ids"]):
            found_msg = msg
            break
            
    if found_msg:
        logging.debug(f"🎭 REACTION: {actor_key} reaction on {message_id} -> {emojis}")
        globals.chat_histories[chat_id].append({
            "role": "user",
            "content": f"[Reaction update: {actor_key} reacted to message ID {message_id} with {', '.join(emojis) if emojis else 'no reaction'}]",
            "timestamp": datetime.now(USER_TIMEZONE),
        })
        
    # Trigger response evaluation if the user added/changed their reaction
    if not is_bot:
        old_emojis = []
        for r in reaction_update.old_reaction:
            if r.type == "emoji":
                old_emojis.append(r.emoji)
            elif r.type == "custom_emoji":
                old_emojis.append("✨")
                
        if set(emojis) != set(old_emojis):
            if emojis:
                logging.debug(f"🎭 REACTION: Triggering evaluation for reaction {emojis} on {message_id}")
                asyncio.create_task(handle_user_reaction_trigger(chat_id, message_id, emojis, user.id))

async def handle_user_reaction_trigger(chat_id: int, message_id: int, emojis: list[str], user_id: int):
    """Invokes the engine contextually when a user reacts to a message."""
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.current_user_id.set(user_id)

    message_thread_id = None
    for history_message in globals.chat_histories.get(chat_id, []):
        if history_message.get("message_id") == message_id or (
            isinstance(history_message.get("message_ids"), list)
            and message_id in history_message["message_ids"]
        ):
            message_thread_id = history_message.get("message_thread_id")
            break
    globals.CURRENT_THREAD_ID.set(message_thread_id)
    temporary_response_mode = is_temporary_mode(chat_id, message_thread_id)
    if temporary_response_mode:
        session_context = None
        turn_context = None
        globals.CURRENT_SESSION_CONTEXT.set(None)
        globals.CURRENT_TURN_CONTEXT.set(None)
    else:
        session_context = await get_session_context(chat_id, message_thread_id, user_id)
        turn_context = await get_turn_context(
            f"reaction update: {', '.join(emojis)}",
            user_id,
            session=session_context,
        )
        set_current_context(session_context, turn_context)
    
    msg_text = ""
    for msg in globals.chat_histories.get(chat_id, []):
        if msg.get("message_id") == message_id or (isinstance(msg.get("message_ids"), list) and message_id in msg["message_ids"]):
            msg_text = msg.get("content", "")
            msg_text = clean_thinking_tags(msg_text)
            break
            
    if not msg_text:
        msg_text = "(older message)"
    else:
        msg_text = msg_text[:100] + "..." if len(msg_text) > 100 else msg_text
        
    emoji_str = ", ".join(emojis)
    trigger_content = (
        f"[System Trigger: User reacted with '{emoji_str}' to the message (ID: {message_id}): '{msg_text}'].\n"
        f"[SYSTEM DIRECTIVE: The user just updated their reaction to an earlier message. "
        f"Decide if a text response or a reaction back is natural. "
        f"If a reaction back is appropriate, use the `react_to_message` tool. "
        f"If no text response is necessary, you MUST reply with exactly 'DONE' to remain silent.]"
    )
    if temporary_response_mode:
        trigger_content = f"[User reaction: '{emoji_str}' on message ID {message_id}: '{msg_text}']"
    
    globals.current_user_id.set(user_id)
    trigger_msg = {
        "role": "user",
        "content": trigger_content,
        "user_id": user_id,
        "is_reaction_trigger": True,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    
    globals.chat_histories[chat_id].append(trigger_msg)
    
    typing_stop = asyncio.Event()

    async def keep_typing():
        while not typing_stop.is_set():
            try:
                await globals.application_bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logging.debug(f"Typing action failed: {e}")
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    
    try:
        reaction_history = globals.chat_histories[chat_id]
        if temporary_response_mode:
            reaction_history = temporary_history(reaction_history, chat_id, message_thread_id)
        response_text, voice_sent_via_tool = await emery_engine(
            reaction_history,
            allow_tools=not temporary_response_mode,
            raw_mode=temporary_response_mode,
            session_context=(globals.CURRENT_SESSION_CONTEXT.get().prompt
                             if globals.CURRENT_SESSION_CONTEXT.get() else None),
            turn_context=(globals.CURRENT_TURN_CONTEXT.get().prompt
                          if globals.CURRENT_TURN_CONTEXT.get() else None),
        )
    finally:
        typing_stop.set()
        await typing_task
        
    clean_response = clean_thinking_tags(response_text).strip()
    
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.debug("🤫 REACTION: Suppressed response (model chose silence)")
        globals.chat_histories[chat_id].append({
            "role": "assistant",
            "content": response_text,
            "timestamp": datetime.now(USER_TIMEZONE)
        })
        await _clear_completed_turn_scratchpad(temporary_response_mode)
        return
        
    globals.chat_histories[chat_id].append({
        "role": "assistant",
        "content": response_text,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    try:
        delivered_response = (
            f"🕶️ Temporary mode — no long-term memory.\n\n{clean_response}"
            if temporary_response_mode else clean_response
        )
        sent_msgs = await send_model_text_message_as_reply(chat_id, delivered_response, reply_to_message_id=message_id)
        if sent_msgs:
            last_entry = globals.chat_histories[chat_id][-1]
            last_entry["message_ids"] = [m.message_id for m in sent_msgs]
            last_entry["message_id"] = sent_msgs[-1].message_id
            await _clear_completed_turn_scratchpad(temporary_response_mode)
    except Exception as e:
        logging.error(f"Failed to send reaction reply: {e}")

def _is_heartbeat_sleep_window(now: datetime) -> bool:
    start_h, start_m = map(int, HEARTBEAT_SLEEP_START.split(':'))
    end_h, end_m = map(int, HEARTBEAT_SLEEP_END.split(':'))
    start_time = time(start_h, start_m)
    end_time = time(end_h, end_m)
    curr_time = now.time()

    if start_time <= end_time:
        return start_time <= curr_time <= end_time
    return curr_time >= start_time or curr_time <= end_time


def _heartbeat_daily_count(chat_id: int, now: datetime) -> int:
    date_key = now.date().isoformat()
    state = _heartbeat_daily_proactive_counts.get(chat_id)
    if not state or state.get("date") != date_key:
        state = {"date": date_key, "count": 0}
        _heartbeat_daily_proactive_counts[chat_id] = state
    return state["count"]


def _record_heartbeat_proactive(chat_id: int, now: datetime) -> None:
    _heartbeat_last_proactive[chat_id] = now
    date_key = now.date().isoformat()
    state = _heartbeat_daily_proactive_counts.get(chat_id)
    if not state or state.get("date") != date_key:
        state = {"date": date_key, "count": 0}
        _heartbeat_daily_proactive_counts[chat_id] = state
    state["count"] += 1


def _seconds_since(now: datetime, past: datetime) -> float:
    return max((now - past).total_seconds(), 0)


def _heartbeat_suppression_reason(chat_id: int, now: datetime) -> str:
    if HEARTBEAT_DAILY_PROACTIVE_LIMIT > 0 and _heartbeat_daily_count(chat_id, now) >= HEARTBEAT_DAILY_PROACTIVE_LIMIT:
        return f"daily proactive limit reached ({HEARTBEAT_DAILY_PROACTIVE_LIMIT})"

    last_proactive = _heartbeat_last_proactive.get(chat_id)
    if last_proactive and _seconds_since(now, last_proactive) < HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS:
        return "proactive message cooldown active"

    last_evaluation = _heartbeat_last_evaluation.get(chat_id)
    if last_evaluation and _seconds_since(now, last_evaluation) < HEARTBEAT_SILENT_RETRY_SECONDS:
        return "silent retry cooldown active"

    return ""


def _last_chat_activity(history) -> datetime | None:
    for msg in reversed(history):
        if msg.get("is_heartbeat_trigger") or msg.get("is_reaction_trigger"):
            continue
        if msg.get("role") not in {"user", "assistant"}:
            continue
        timestamp = msg.get("timestamp")
        if timestamp:
            return timestamp
    return None


def _last_user_id_from_history(history) -> int | None:
    for msg in reversed(history):
        if msg.get("role") == "user" and not msg.get("is_heartbeat_trigger") and msg.get("user_id"):
            return msg.get("user_id")
    return globals.current_user_id.get()


def _clean_heartbeat_text(text: str, max_len: int = 220) -> str:
    text = clean_thinking_tags(str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip() + "..."
    return text


def _is_excluded_heartbeat_context(text: str) -> bool:
    return bool(_HEARTBEAT_EXCLUDED_CONTEXT_RE.search(text or ""))


def _extract_heartbeat_hooks(history, max_hooks: int = 5) -> list[str]:
    hooks = []
    seen = set()
    for msg in reversed(list(history)[-30:]):
        if msg.get("is_heartbeat_trigger") or msg.get("is_reaction_trigger"):
            continue
        if msg.get("role") not in {"user", "assistant"}:
            continue

        content = _clean_heartbeat_text(msg.get("content"))
        if not content or _is_excluded_heartbeat_context(content):
            continue
        if not _HEARTBEAT_HOOK_RE.search(content):
            continue

        sender = msg.get("sender_name") or ("Assistant" if msg.get("role") == "assistant" else "User")
        hook = f"- {sender}: {content}"
        hook_key = hook.lower()
        if hook_key in seen:
            continue
        hooks.append(hook)
        seen.add(hook_key)
        if len(hooks) >= max_hooks:
            break

    hooks.reverse()
    return hooks


def _filter_heartbeat_memory(memory_text: str, max_lines: int = 12) -> str:
    lines = []
    for raw_line in str(memory_text or "").splitlines():
        line = raw_line.strip()
        if not line or _is_excluded_heartbeat_context(line):
            continue
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def _format_heartbeat_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def build_heartbeat_context_packet(chat_id: int, now: datetime, silence_seconds: float) -> str:
    history = globals.chat_histories.get(chat_id, [])
    hooks = _extract_heartbeat_hooks(history)

    user_id = _last_user_id_from_history(history)
    memory_context = ""
    if user_id:
        try:
            memory_query = (
                "heartbeat check-in: unresolved follow-ups, recent projects, household plans, "
                "important personal context, and natural reasons to circle back"
            )
            memory_context = _filter_heartbeat_memory(await retrieve_relevant_memories(memory_query, user_id))
        except Exception as e:
            logging.warning("⚠️ HEARTBEAT: Unable to retrieve memory context: %s", e)

    weather_alerts = ""
    if ENABLE_WEATHER:
        try:
            weather_alerts = await get_noaa_weather_alerts()
        except Exception as e:
            logging.warning("⚠️ HEARTBEAT: Unable to retrieve weather alerts: %s", e)

    daily_count = _heartbeat_daily_count(chat_id, now)
    packet = [
        "Heartbeat context:",
        f"- Silence duration: {_format_heartbeat_duration(silence_seconds)}",
        f"- Proactive messages sent today: {daily_count} / {HEARTBEAT_DAILY_PROACTIVE_LIMIT}",
        "",
        "Recent conversation hooks:",
        "\n".join(hooks) if hooks else "None found.",
        "",
        "Relevant memory/history:",
        memory_context if memory_context else "None found.",
        "",
        "Weather alerts:",
        weather_alerts if weather_alerts else "None.",
    ]
    return "\n".join(packet)


async def heartbeat_check(context: ContextTypes.DEFAULT_TYPE):
    """Callback for Telegram JobQueue that runs periodically to check if the bot should spontaneously send a message."""
    if not ENABLE_HEARTBEAT:
        return
        
    logging.debug("💓 HEARTBEAT: Checking activity...")
    
    if TELEGRAM_GROUP_CHAT_ID is None:
        logging.debug("💓 HEARTBEAT: TELEGRAM_GROUP_CHAT_ID not set, skipping check.")
        return
    group_chat_id = TELEGRAM_GROUP_CHAT_ID
    if is_chat_temporary_mode(group_chat_id):
        logging.debug("💓 HEARTBEAT: Suppressed because temporary mode is active.")
        return
        
    history = globals.chat_histories.get(group_chat_id)
    if not history:
        return
        
    now = datetime.now(USER_TIMEZONE)
    
    # Check if current time falls within user's sleep window
    try:
        if _is_heartbeat_sleep_window(now):
            logging.debug(f"💓 HEARTBEAT: Suppressed check-in (inside sleep window: {HEARTBEAT_SLEEP_START}-{HEARTBEAT_SLEEP_END})")
            return
    except Exception as e:
        logging.error(f"❌ HEARTBEAT: Error checking sleep window range: {e}")
        
    last_time = _last_chat_activity(history)
    if not last_time:
        return
        
    elapsed = (now - last_time).total_seconds()
    if elapsed > HEARTBEAT_SILENCE_THRESHOLD_SECONDS:
        suppression_reason = _heartbeat_suppression_reason(group_chat_id, now)
        if suppression_reason:
            logging.debug("💓 HEARTBEAT: Suppressed check-in (%s).", suppression_reason)
            return

        logging.info(f"💓 HEARTBEAT: Chat {group_chat_id} silent for {elapsed:.1f}s, evaluating check-in...")
        _heartbeat_last_evaluation[group_chat_id] = now
        await handle_heartbeat_trigger(group_chat_id, elapsed)

async def handle_heartbeat_trigger(chat_id: int, silence_seconds: float = None):
    """Triggers the model to potentially circle back or check in on a silent chat."""
    if is_chat_temporary_mode(chat_id):
        return
    globals.TARGET_CHAT_ID.set(chat_id)
    
    # Determine the topic/thread ID for the heartbeats
    message_thread_id = None
    if CHAT_TOPIC_ID is not None:
        message_thread_id = CHAT_TOPIC_ID
            
    if message_thread_id is None and globals.chat_histories.get(chat_id):
        for msg in reversed(globals.chat_histories[chat_id]):
            if msg.get("message_id") and msg.get("message_thread_id"):
                message_thread_id = msg.get("message_thread_id")
                break
                
    globals.CURRENT_THREAD_ID.set(message_thread_id)

    now = datetime.now(USER_TIMEZONE)
    if silence_seconds is None:
        last_time = _last_chat_activity(globals.chat_histories.get(chat_id, []))
        silence_seconds = _seconds_since(now, last_time) if last_time else 0

    context_packet = await build_heartbeat_context_packet(chat_id, now, silence_seconds)

    heartbeat_user_id = _last_user_id_from_history(globals.chat_histories.get(chat_id, []))
    if heartbeat_user_id is not None:
        globals.current_user_id.set(heartbeat_user_id)
    session_context = await get_session_context(
        chat_id,
        message_thread_id,
        heartbeat_user_id,
        session_variant="heartbeat",
    )
    turn_context = await get_turn_context(
        "heartbeat check-in",
        heartbeat_user_id,
        session=session_context,
    )
    set_current_context(session_context, turn_context)
    
    trigger_content = (
        f"[System Trigger (Heartbeat)]: It has been several hours since the last message in this chat. "
        f"Review the conversation history and the private context below. Be selective and human-like. "
        f"Send one short, natural message only if there is a timely, personally relevant, or socially natural reason to check in. "
        f"Good reasons include an unresolved thread, a prior promise to follow up, a relevant remembered topic, or an active weather alert. "
        f"Do not summarize the private context, mention the trigger, or perform additional lookups. "
        f"If the message would be generic filler, or if the conversation has reached a natural pause, "
        f"you MUST reply with exactly 'DONE' to remain completely silent.\n\n{context_packet}"
    )
    
    trigger_msg = {
        "role": "user",
        "content": trigger_content,
        "is_heartbeat_trigger": True,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    
    globals.chat_histories[chat_id].append(trigger_msg)
    
    try:
        response_text, voice_sent_via_tool = await emery_engine(
            globals.chat_histories[chat_id],
            allow_tools=False,
            session_context=(globals.CURRENT_SESSION_CONTEXT.get().prompt
                             if globals.CURRENT_SESSION_CONTEXT.get() else None),
            turn_context=(globals.CURRENT_TURN_CONTEXT.get().prompt
                          if globals.CURRENT_TURN_CONTEXT.get() else None),
        )
    except Exception as e:
        logging.error(f"Error executing heartbeat engine: {e}")
        response_text = "DONE"
        
    clean_response = clean_thinking_tags(response_text).strip()
    
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.debug(f"🤫 HEARTBEAT: Chat {chat_id} remains silent.")
        globals.chat_histories[chat_id].append({
            "role": "assistant",
            "content": response_text,
            "message_thread_id": message_thread_id,
            "timestamp": datetime.now(USER_TIMEZONE),
        })
        return

    if not clean_response:
        logging.debug(f"🤫 HEARTBEAT: Chat {chat_id} produced an empty response; remaining silent.")
        globals.chat_histories[chat_id].append({
            "role": "assistant",
            "content": response_text,
            "message_thread_id": message_thread_id,
            "timestamp": datetime.now(USER_TIMEZONE),
        })
        return
        
    reply_to_id = None
    for msg in reversed(globals.chat_histories[chat_id]):
        if msg.get("message_id"):
            reply_to_id = msg.get("message_id")
            break
            
    try:
        sent_msgs = await send_model_text_message_as_reply(chat_id, clean_response, reply_to_id, message_thread_id)
        if sent_msgs:
            _record_heartbeat_proactive(chat_id, datetime.now(USER_TIMEZONE))
            globals.chat_histories[chat_id].append({
                "role": "assistant",
                "content": response_text,
                "message_thread_id": message_thread_id,
                "timestamp": datetime.now(USER_TIMEZONE),
                "message_ids": [m.message_id for m in sent_msgs],
                "message_id": sent_msgs[-1].message_id,
            })
    except Exception as e:
        logging.error(f"Failed to send heartbeat message: {e}")

async def start_bot_heartbeat(application) -> None:
    """Registers the bot heartbeat job in the Telegram JobQueue on startup."""
    if not ENABLE_HEARTBEAT:
        logging.debug("💓 HEARTBEAT: Spontaneous heartbeat disabled.")
        return
        
    if not application.job_queue:
        logging.warning("⚠️ HEARTBEAT: JobQueue is not available. Heartbeat cannot be registered.")
        return
        
    application.job_queue.run_repeating(
        heartbeat_check,
        interval=HEARTBEAT_INTERVAL_SECONDS,
        first=60,
        name="bot_heartbeat"
    )
    logging.info(f"💓 HEARTBEAT: Active (checking every {HEARTBEAT_INTERVAL_SECONDS}s)")

async def bot_post_init(application) -> None:
    """Consolidated post_init wrapper to launch Reolink polling and start the bot heartbeat."""
    from emery.inter_agent_bridge import initialize_inter_agent_bridge

    # Reconcile process-backed terminal/browser resources before accepting
    # turns.  The durable layer retains sanitized metadata but never attempts
    # to reattach a PTY or claim an unrelated browser target.
    try:
        from emery.session_persistence import recover_stale_resources
        recovery = await recover_stale_resources()
        if recovery:
            logging.info("SESSION RECOVERY: reconciled %d stale execution resources", len(recovery))
    except Exception as exc:
        # Startup should remain available if a broker is temporarily down;
        # failed resources remain recovery_pending and will be retried by the
        # next terminal/browser tool call.
        logging.warning("SESSION RECOVERY: startup reconciliation deferred: %s", exc)

    await initialize_inter_agent_bridge(application)
    await validate_telegram_routing(application)
    from emery.tools import start_reolink_polling
    await start_reolink_polling(application)
    await start_bot_heartbeat(application)
    
    # Preload sticker set if configured in environment
    sticker_set_name = TELEGRAM_STICKER_SET
    if sticker_set_name:
        try:
            sticker_set = await application.bot.get_sticker_set(sticker_set_name)
            for sticker in sticker_set.stickers:
                if sticker.emoji:
                    globals.learned_stickers[sticker.emoji] = sticker.file_id
            logging.info(f"🎨 STICKERS: Preloaded {len(sticker_set.stickers)} stickers from '{sticker_set_name}'")
        except Exception as e:
            logging.error(f"⚠️ STICKERS: Failed to preload sticker set '{sticker_set_name}': {e}")


async def bot_post_shutdown(application) -> None:
    """Release Emery-owned browser processes on graceful application exit."""
    del application
    try:
        from emery.browser_control import close_browser
        result = await close_browser()
        closed = result.get("browser_session_results") or []
        if closed:
            from emery.session_persistence import get_session_integration
            integration = get_session_integration()
            for item in closed:
                session_id = item.get("browser_session_id")
                if session_id:
                    integration.update_resource(
                        "browser_session",
                        session_id,
                        status="closed",
                        metadata={"session": item.get("session", item)},
                    )
        logging.info("BROWSER SHUTDOWN: released Emery-owned browser resources")
    except Exception as exc:
        logging.warning("BROWSER SHUTDOWN: cleanup failed: %s", exc)
