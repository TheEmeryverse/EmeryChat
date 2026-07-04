"""Telegram Bot API bridge for authenticated Emery/Hermes messages.

Each process uses its own ``python-telegram-bot`` ``Bot`` instance.  The Bot
API token therefore authenticates the sending account; tokens are never sent
over the bridge or shared with the peer.

Wire format::

    /bridge {"v":1,"t":"request|response","id":"...","src":1,
             "dst":2,"h":0,"body":"<base64url UTF-8>"}

Humans may use the shorter ``/bridge <message>`` form.  Bot-to-bot mode must
be enabled for both accounts in BotFather before Telegram will deliver direct
bot messages.
"""

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from emery.config import (
    ALLOWED_BOT_IDS,
    ALLOWED_USER_IDS,
    ALLOW_UNRESTRICTED_TELEGRAM_ACCESS,
    MODEL_ID,
    USER_TIMEZONE,
)


HERMES_BOT_ID = 8726427681
HERMES_BOT_USERNAME = "@emeryverse_hermesbot"
BRIDGE_VERSION = 1
MAX_BRIDGE_HOPS = 1
MAX_PAYLOAD_BYTES = 2800
PENDING_TTL_SECONDS = 15 * 60
BRIDGE_RESPONSE_TIMEOUT_SECONDS = 10 * 60
PEER_RATE_LIMIT_REQUESTS = 5
PEER_RATE_LIMIT_WINDOW_SECONDS = 60

_BRIDGE_COMMAND_RE = re.compile(r"^/bridge(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.DOTALL)


def _encode_body(body: str) -> str:
    return base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")


def _decode_body(body: str) -> str:
    return base64.b64decode(body.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")


def _limit_payload(body: str) -> str:
    encoded = str(body or "").encode("utf-8")
    if len(encoded) <= MAX_PAYLOAD_BYTES:
        return str(body or "")

    suffix = "\n[bridge payload truncated]"
    available = MAX_PAYLOAD_BYTES - len(suffix.encode("utf-8"))
    return encoded[:available].decode("utf-8", errors="ignore") + suffix


@dataclass(frozen=True)
class BridgeEnvelope:
    message_type: str
    request_id: str
    source_bot_id: int
    destination_bot_id: int
    hops: int
    body: str

    def to_command(self) -> str:
        payload = {
            "v": BRIDGE_VERSION,
            "t": self.message_type,
            "id": self.request_id,
            "src": self.source_bot_id,
            "dst": self.destination_bot_id,
            "h": self.hops,
            "body": _encode_body(_limit_payload(self.body)),
        }
        return "/bridge " + json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_command(cls, text: str) -> "BridgeEnvelope":
        match = _BRIDGE_COMMAND_RE.match(str(text or "").strip())
        if not match or not match.group(1):
            raise ValueError("missing bridge envelope")

        payload = json.loads(match.group(1))
        if payload.get("v") != BRIDGE_VERSION:
            raise ValueError("unsupported bridge protocol version")
        if payload.get("t") not in {"request", "response"}:
            raise ValueError("invalid bridge message type")

        request_id = str(payload.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id):
            raise ValueError("invalid bridge request ID")

        envelope = cls(
            message_type=payload["t"],
            request_id=request_id,
            source_bot_id=int(payload["src"]),
            destination_bot_id=int(payload["dst"]),
            hops=int(payload["h"]),
            body=_decode_body(str(payload["body"])),
        )
        if envelope.hops < 0 or envelope.hops > MAX_BRIDGE_HOPS:
            raise ValueError("bridge hop limit exceeded")
        if len(envelope.body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("bridge payload is too large")
        return envelope


@dataclass(frozen=True)
class _PendingRoute:
    chat_id: int
    message_thread_id: int | None
    created_at: float
    response_future: asyncio.Future[str] | None = None


@dataclass(frozen=True)
class _CachedResponse:
    envelope: BridgeEnvelope
    created_at: float


class InterAgentBridge:
    """Routes correlated requests and responses between two Telegram bots."""

    def __init__(
        self,
        local_bot_id: int | None,
        peer_bot_id: int,
        *,
        peer_chat_id: int | str | None = None,
    ):
        self.local_bot_id = int(local_bot_id) if local_bot_id is not None else None
        self.peer_bot_id = int(peer_bot_id)
        self.peer_chat_id = peer_chat_id or self.peer_bot_id
        self._authenticated = False
        self._pending: dict[str, _PendingRoute] = {}
        self._completed: dict[str, _CachedResponse] = {}
        self._inflight: set[str] = set()
        self._peer_requests: dict[int, deque[float]] = {}

    async def authenticate(self, bot) -> None:
        """Verify that the configured Bot API token belongs to this endpoint."""
        identity = await bot.get_me()
        if not identity.is_bot:
            raise RuntimeError("Bridge authentication requires a Telegram bot account.")
        if self.local_bot_id is not None and identity.id != self.local_bot_id:
            raise RuntimeError(
                f"Bridge expected bot ID {self.local_bot_id}, but the configured token "
                f"authenticated bot ID {identity.id}."
            )
        self.local_bot_id = identity.id
        self._authenticated = True
        logging.info(
            "INTER-AGENT BRIDGE: authenticated bot_id=%s; peer_bot_id=%s",
            self.local_bot_id,
            self.peer_bot_id,
        )

    async def _ensure_authenticated(self, bot) -> None:
        if not self._authenticated:
            await self.authenticate(bot)

    def _prune_state(self) -> None:
        now = time.monotonic()
        cutoff = now - PENDING_TTL_SECONDS
        expired_routes = {
            request_id: route
            for request_id, route in self._pending.items()
            if route.created_at < cutoff
        }
        for route in expired_routes.values():
            future = route.response_future
            if future is not None and not future.done():
                future.set_exception(asyncio.TimeoutError("bridge response timed out"))

        self._pending = {
            request_id: route
            for request_id, route in self._pending.items()
            if route.created_at >= cutoff
        }
        self._completed = {
            request_id: cached
            for request_id, cached in self._completed.items()
            if cached.created_at >= cutoff
        }
        rate_cutoff = now - PEER_RATE_LIMIT_WINDOW_SECONDS
        for peer_id, requests in list(self._peer_requests.items()):
            while requests and requests[0] < rate_cutoff:
                requests.popleft()
            if not requests:
                self._peer_requests.pop(peer_id, None)

    def _enforce_peer_rate_limit(self, peer_id: int) -> None:
        now = time.monotonic()
        cutoff = now - PEER_RATE_LIMIT_WINDOW_SECONDS
        requests = self._peer_requests.setdefault(peer_id, deque())
        while requests and requests[0] < cutoff:
            requests.popleft()
        if len(requests) >= PEER_RATE_LIMIT_REQUESTS:
            raise ValueError("bridge peer rate limit exceeded")
        requests.append(now)

    async def _send_envelope(self, bot, envelope: BridgeEnvelope) -> None:
        await self._ensure_authenticated(bot)
        if envelope.source_bot_id != self.local_bot_id:
            raise ValueError("outgoing bridge envelope has the wrong source bot")
        if envelope.destination_bot_id != self.peer_bot_id:
            raise ValueError("outgoing bridge envelope has the wrong destination bot")
        await bot.send_message(chat_id=self.peer_chat_id, text=envelope.to_command())

    async def send_request(
        self,
        bot,
        body: str,
        *,
        origin_chat_id: int,
        origin_thread_id: int | None = None,
        response_future: asyncio.Future[str] | None = None,
    ) -> str:
        body = str(body or "").strip()
        if not body:
            raise ValueError("bridge message cannot be empty")
        if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"bridge message exceeds {MAX_PAYLOAD_BYTES} UTF-8 bytes")

        await self._ensure_authenticated(bot)
        self._prune_state()
        request_id = uuid.uuid4().hex
        self._pending[request_id] = _PendingRoute(
            chat_id=origin_chat_id,
            message_thread_id=origin_thread_id,
            created_at=time.monotonic(),
            response_future=response_future,
        )
        envelope = BridgeEnvelope(
            message_type="request",
            request_id=request_id,
            source_bot_id=self.local_bot_id,
            destination_bot_id=self.peer_bot_id,
            hops=0,
            body=body,
        )
        try:
            await self._send_envelope(bot, envelope)
        except Exception:
            self._pending.pop(request_id, None)
            raise
        return request_id

    async def send_request_and_wait(
        self,
        bot,
        body: str,
        *,
        origin_chat_id: int,
        origin_thread_id: int | None = None,
        timeout: float = BRIDGE_RESPONSE_TIMEOUT_SECONDS,
    ) -> str:
        """Send a request and return the correlated peer response body."""
        response_future = asyncio.get_running_loop().create_future()
        try:
            request_id = await self.send_request(
                bot,
                body,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                response_future=response_future,
            )
        except Exception:
            response_future.cancel()
            raise
        try:
            return await asyncio.wait_for(asyncio.shield(response_future), timeout=timeout)
        finally:
            route = self._pending.get(request_id)
            if route is not None and route.response_future is response_future:
                self._pending.pop(request_id, None)
            if not response_future.done():
                response_future.cancel()

    def _validate_peer_envelope(self, envelope: BridgeEnvelope, sender_id: int) -> None:
        if sender_id != self.peer_bot_id:
            raise ValueError("bridge sender is not the configured peer")
        if envelope.source_bot_id != sender_id:
            raise ValueError("bridge envelope source does not match Telegram sender")
        if envelope.destination_bot_id != self.local_bot_id:
            raise ValueError("bridge envelope is addressed to another bot")

    async def _process_request(
        self,
        bot,
        envelope: BridgeEnvelope,
        *,
        sender_id: int,
        sender_name: str,
    ) -> None:
        await self._ensure_authenticated(bot)
        self._prune_state()
        self._validate_peer_envelope(envelope, sender_id)
        if envelope.hops >= MAX_BRIDGE_HOPS:
            raise ValueError("bridge request cannot be relayed beyond the hop limit")
        cached = self._completed.get(envelope.request_id)
        if cached is not None:
            logging.info("INTER-AGENT BRIDGE: replaying response for duplicate request %s", envelope.request_id)
            await self._send_envelope(bot, cached.envelope)
            return
        if envelope.request_id in self._inflight:
            logging.warning("INTER-AGENT BRIDGE: ignored in-flight duplicate request %s", envelope.request_id)
            return
        self._enforce_peer_rate_limit(sender_id)
        self._inflight.add(envelope.request_id)

        try:
            try:
                response = await _run_emery_request(
                    envelope.body,
                    sender_id=envelope.source_bot_id,
                    sender_name=sender_name,
                )
            except Exception:
                logging.exception("INTER-AGENT BRIDGE: request %s failed", envelope.request_id)
                response = "EmeryChat failed to process the bridge request."

            response_envelope = BridgeEnvelope(
                message_type="response",
                request_id=envelope.request_id,
                source_bot_id=self.local_bot_id,
                destination_bot_id=self.peer_bot_id,
                hops=envelope.hops + 1,
                body=_limit_payload(response),
            )
            self._completed[envelope.request_id] = _CachedResponse(
                envelope=response_envelope,
                created_at=time.monotonic(),
            )
            await self._send_envelope(bot, response_envelope)
        finally:
            self._inflight.discard(envelope.request_id)

    async def _route_response(self, bot, envelope: BridgeEnvelope) -> None:
        self._prune_state()
        route = self._pending.get(envelope.request_id)
        if route is None:
            logging.warning(
                "INTER-AGENT BRIDGE: response %s has no pending local request",
                envelope.request_id,
            )
            return

        if route.response_future is not None:
            if not route.response_future.done():
                route.response_future.set_result(envelope.body)
            self._pending.pop(envelope.request_id, None)
            return

        await bot.send_message(
            chat_id=route.chat_id,
            message_thread_id=route.message_thread_id,
            text=f"Hermes:\n{envelope.body}",
        )
        self._pending.pop(envelope.request_id, None)

    async def handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        sender = update.effective_user
        if not message or not sender:
            return

        text = message.text or ""
        command_match = _BRIDGE_COMMAND_RE.match(text.strip())
        command_body = command_match.group(1).strip() if command_match and command_match.group(1) else ""

        if sender.is_bot and sender.id == self.peer_bot_id:
            if sender.id not in ALLOWED_BOT_IDS:
                logging.warning("INTER-AGENT BRIDGE: rejected non-allowlisted peer bot %s", sender.id)
                return
            try:
                envelope = BridgeEnvelope.from_command(text)
                self._validate_peer_envelope(envelope, sender.id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logging.warning("INTER-AGENT BRIDGE: rejected malformed envelope: %s", exc)
                return

            if envelope.message_type == "request":
                await self._process_request(
                    context.bot,
                    envelope,
                    sender_id=sender.id,
                    sender_name=sender.first_name or "Hermes",
                )
            else:
                await self._route_response(context.bot, envelope)
            return

        if sender.is_bot:
            return
        if not _is_allowed_human(sender.id):
            return
        if not command_body:
            await message.reply_text("Usage: /bridge <message>")
            return

        try:
            request_id = await self.send_request(
                context.bot,
                command_body,
                origin_chat_id=update.effective_chat.id,
                origin_thread_id=message.message_thread_id,
            )
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        except TelegramError as exc:
            logging.warning("INTER-AGENT BRIDGE: request delivery failed: %s", exc)
            await message.reply_text(
                "Bridge delivery failed. Confirm bot-to-bot communication is enabled for both bots."
            )
            return

        await message.reply_text(f"Bridge request sent to Hermes ({request_id[:8]}).")

    async def handle_direct_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Treat a plain direct message from the peer bot as a bridge request."""
        message = update.effective_message
        sender = update.effective_user
        if not message or not sender or not sender.is_bot or sender.id != self.peer_bot_id:
            return
        if sender.id not in ALLOWED_BOT_IDS:
            return

        body = str(message.text or "").strip()
        if not body:
            return
        envelope = BridgeEnvelope(
            message_type="request",
            request_id=uuid.uuid4().hex,
            source_bot_id=sender.id,
            destination_bot_id=self.local_bot_id,
            hops=0,
            body=_limit_payload(body),
        )
        await self._process_request(
            context.bot,
            envelope,
            sender_id=sender.id,
            sender_name=sender.first_name or "Hermes",
        )


def _is_allowed_human(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return bool(ALLOW_UNRESTRICTED_TELEGRAM_ACCESS)
    return user_id in ALLOWED_USER_IDS


async def _run_emery_request(body: str, *, sender_id: int, sender_name: str) -> str:
    """Run one peer request through Emery's existing engine and peer history."""
    import emery.globals as globals
    from emery.engine import emery_engine
    from emery.helpers import clean_thinking_tags, get_current_system_prompt

    chat_id = sender_id
    if chat_id not in globals.chat_histories:
        globals.chat_histories[chat_id] = deque()
    history = globals.chat_histories[chat_id]

    user_token = globals.current_user_id.set(sender_id)
    chat_token = globals.TARGET_CHAT_ID.set(chat_id)
    thread_token = globals.CURRENT_THREAD_ID.set(None)
    try:
        now = datetime.now(USER_TIMEZONE)
        runtime_context = await get_current_system_prompt(body, sender_id)
        history.append(
            {
                "role": "user",
                "content": (
                    f"{runtime_context}\n\n# Inter-agent Message\n"
                    f"[{now.strftime('%A, %B %d, %Y at %I:%M %p')}] "
                    f"{sender_name}: {body}"
                ),
                "user_id": sender_id,
                "sender_name": sender_name,
                "timestamp": now,
                "is_bridge_message": True,
            }
        )
        response, _ = await emery_engine(history, model_to_use=MODEL_ID)
        clean_response = clean_thinking_tags(response).strip() or "DONE"
        history.append(
            {
                "role": "assistant",
                "content": response,
                "timestamp": datetime.now(USER_TIMEZONE),
                "is_bridge_message": True,
            }
        )
        return clean_response
    finally:
        globals.CURRENT_THREAD_ID.reset(thread_token)
        globals.TARGET_CHAT_ID.reset(chat_token)
        globals.current_user_id.reset(user_token)


_bridge = InterAgentBridge(
    local_bot_id=None,
    peer_bot_id=HERMES_BOT_ID,
    peer_chat_id=HERMES_BOT_USERNAME,
)


async def initialize_inter_agent_bridge(application) -> None:
    await _bridge.authenticate(application.bot)


async def handle_bridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _bridge.handle_command(update, context)


async def handle_bridge_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _bridge.handle_direct_message(update, context)
