import asyncio
import html
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from emery.config import (
    DEBATE_ARCHIVE_DIR,
    DEBATE_INDEX_PATH,
    ENABLE_YOUTUBE_TRANSCRIPT,
    EXPERT_FAST_ENABLE_THINKING,
    EXPERT_FAST_MAX_TOKENS,
    EXPERT_FAST_MIN_P,
    EXPERT_FAST_PRESENCE_PENALTY,
    EXPERT_FAST_REPETITION_PENALTY,
    EXPERT_FAST_TEMPERATURE,
    EXPERT_FAST_TOP_K,
    EXPERT_FAST_TOP_P,
    EXPERT_MAIN_ENABLE_THINKING,
    EXPERT_MAIN_MAX_TOKENS,
    EXPERT_MAIN_MIN_P,
    EXPERT_MAIN_PRESENCE_PENALTY,
    EXPERT_MAIN_REPETITION_PENALTY,
    EXPERT_MAIN_TEMPERATURE,
    EXPERT_MAIN_TOP_K,
    EXPERT_MAIN_TOP_P,
    FAST_MODEL_ID,
    FAST_MODEL_URL,
    MAIN_MODEL_URL,
    MODEL_ID,
    SEARXNG_URL,
    USER_TIMEZONE,
)
from emery.logging_utils import format_logging_payload, safe_preview
from emery.telegram_delivery import send_split_html_message
from emery.telegram_utils import normalize_message_thread_id
from emery.tools import fetch_web_content, get_youtube_transcript
import emery.globals as globals


ACTIVE_DEBATES: dict[tuple[int, int | None], "DebateSession"] = {}
DEBATE_TASKS: dict[str, asyncio.Task] = {}

CALLBACK_PREFIX = "debate"
INITIAL_POSITION_SOURCE_LIMIT = 2
SIDE_LIGHT_SOURCE_LIMIT = 3
SIDE_DEEP_SOURCE_LIMIT = 10
ROUND_SOURCE_LIMIT = 2
MIN_DEBATE_ROUNDS = 3
MAX_DEBATE_ROUNDS = 5
SEARCH_RESULTS_PER_QUERY = 8


@dataclass
class DebateSession:
    id: str
    topic: str
    chat_id: int
    message_thread_id: int | None
    user_id: int | None
    status: str = "preparing_positions"
    created_at: str = field(default_factory=lambda: _now_iso())
    updated_at: str = field(default_factory=lambda: _now_iso())
    positions: dict = field(default_factory=dict)
    side_names: dict = field(default_factory=dict)
    advocate_names: dict = field(default_factory=dict)
    position_framing: str = ""
    user_inputs: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    search_queries: list[dict] = field(default_factory=list)
    research_packets: list[dict] = field(default_factory=list)
    role_briefs: dict = field(default_factory=dict)
    formal_turns: list[dict] = field(default_factory=list)
    debate_questions: list[str] = field(default_factory=list)
    round_results: list[dict] = field(default_factory=list)
    final_memo: str = ""
    source_appendix: str = ""
    archive_path: str = ""
    archive_label: str = ""

    def key(self) -> tuple[int, int | None]:
        return self.chat_id, normalize_message_thread_id(self.chat_id, self.message_thread_id)

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DebateSession":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})


def _now_iso() -> str:
    return datetime.now(USER_TIMEZONE).replace(microsecond=0).isoformat()


def _now_label() -> str:
    return datetime.now(USER_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _session_key(chat_id: int, message_thread_id: int | None) -> tuple[int, int | None]:
    return chat_id, normalize_message_thread_id(chat_id, message_thread_id)


def _slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").lower()).strip("-")
    return (slug[:max_len].strip("-") or "debate")


def _normalize_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _source_domain(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").removeprefix("www.")


def _extract_json_object(text: str):
    clean = _clean_thinking_tags(str(text or "")).strip()
    if not clean:
        return None
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "")


def _clean_thinking_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL | re.IGNORECASE)


def _telegram_escape(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def _clean_side_name(raw: str, fallback: str) -> str:
    clean = re.sub(r"\s+", " ", str(raw or "")).strip()
    clean = re.sub(r"[^\w\s&+/-]", "", clean).strip()
    if not clean:
        return fallback
    words = clean.split()
    return " ".join(words[:4])[:40].strip() or fallback


def _side_name(session: DebateSession, side: str) -> str:
    fallback = "Position 1" if side == "pro" else "Position 2"
    return _clean_side_name((session.side_names or {}).get(side), fallback)


def _advocate_name(session: DebateSession, side: str) -> str:
    fallback = "Jill" if side == "pro" else "Mathias"
    return _clean_side_name((session.advocate_names or {}).get(side), fallback)


def _side_label(session: DebateSession, side: str) -> str:
    return f"{_side_name(session, side)}, argued by {_advocate_name(session, side)}"


async def _query_chat_model(
    prompt: str,
    system_prompt: str,
    *,
    model_id: str,
    url: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    min_p: float,
    presence_penalty: float,
    repetition_penalty: float,
    enable_thinking: bool,
    lock,
) -> str:
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "min_p": min_p,
        "presence_penalty": presence_penalty,
        "repetition_penalty": repetition_penalty,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
    }
    try:
        async with lock:
            response = await globals.http_client.post(url, json=payload, timeout=900)
        if response.status_code != 200:
            logging.error("DEBATE: model endpoint returned HTTP %s: %s", response.status_code, safe_preview(response.text))
            return ""
        data = response.json()
        message = ((data.get("choices") or [{}])[0]).get("message", {})
        return _clean_thinking_tags(_message_content_to_text(message.get("content"))).strip()
    except Exception as exc:
        logging.error("DEBATE: model query failed: %s", exc, exc_info=True)
        return ""


async def _query_main_model(prompt: str, system_prompt: str) -> str:
    return await _query_chat_model(
        prompt,
        system_prompt,
        model_id=MODEL_ID,
        url=MAIN_MODEL_URL,
        max_tokens=EXPERT_MAIN_MAX_TOKENS,
        temperature=EXPERT_MAIN_TEMPERATURE,
        top_p=EXPERT_MAIN_TOP_P,
        top_k=EXPERT_MAIN_TOP_K,
        min_p=EXPERT_MAIN_MIN_P,
        presence_penalty=EXPERT_MAIN_PRESENCE_PENALTY,
        repetition_penalty=EXPERT_MAIN_REPETITION_PENALTY,
        enable_thinking=EXPERT_MAIN_ENABLE_THINKING,
        lock=globals.main_model_lock,
    )


async def _query_clerk_model(prompt: str, system_prompt: str) -> str:
    return await _query_chat_model(
        prompt,
        system_prompt,
        model_id=FAST_MODEL_ID,
        url=FAST_MODEL_URL,
        max_tokens=EXPERT_FAST_MAX_TOKENS,
        temperature=EXPERT_FAST_TEMPERATURE,
        top_p=EXPERT_FAST_TOP_P,
        top_k=EXPERT_FAST_TOP_K,
        min_p=EXPERT_FAST_MIN_P,
        presence_penalty=EXPERT_FAST_PRESENCE_PENALTY,
        repetition_penalty=EXPERT_FAST_REPETITION_PENALTY,
        enable_thinking=EXPERT_FAST_ENABLE_THINKING,
        lock=globals.fast_model_lock,
    )


async def _search_web(query: str) -> list[dict]:
    try:
        response = await globals.http_client.get(SEARXNG_URL, params={"q": query, "format": "json"}, timeout=40)
        results = response.json().get("results") or []
    except Exception as exc:
        logging.warning("DEBATE: Search failed for %r: %s", query, exc)
        return []

    clean = []
    for item in results[:SEARCH_RESULTS_PER_QUERY]:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        clean.append({
            "query": query,
            "title": title,
            "url": url,
            "normalized_url": _normalize_url(url),
            "domain": _source_domain(url),
            "snippet": str(item.get("content") or item.get("snippet") or "").strip(),
        })
    return clean


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return "youtube.com" in host or "youtu.be" in host


async def _fetch_source_content(result: dict) -> dict:
    url = str(result.get("url") or "").strip()
    if ENABLE_YOUTUBE_TRANSCRIPT and _is_youtube_url(url):
        logging.info("DEBATE TOOL: get_youtube_transcript | Args: %s", format_logging_payload({"video_url_or_id": url}))
        transcript = await get_youtube_transcript(url, languages="en", include_timestamps=False)
        transcript_text = str(transcript or "").strip()
        if transcript_text and not transcript_text.lower().startswith("youtube transcript error"):
            return {"success": True, "title": result.get("title") or "YouTube transcript", "url": url, "content": transcript_text}
        return {"success": False, "title": result.get("title") or "YouTube video", "url": url, "error": transcript_text or "Transcript unavailable.", "content": ""}

    logging.info("DEBATE TOOL: fetch_web_content | Args: %s", format_logging_payload({"url": url}))
    return await fetch_web_content(url, max_chars=10000, summarize_long=False)


async def _summarize_source(session: DebateSession, result: dict, fetched: dict) -> dict:
    content = str(fetched.get("content") or "")
    prompt = (
        "Summarize this source for a structured debate clerk packet. Return strict JSON with keys: "
        "summary, key_claims, perspective, reliability_label, relevance_note. Keep it factual and concise. "
        "Focus on why this source is relevant to the debate topic and the research request; if relevance is weak, say so in relevance_note.\n\n"
        f"Debate topic: {session.topic}\n"
        f"Search query: {result.get('query')}\n"
        f"Search snippet: {result.get('snippet')}\n"
        f"Title: {fetched.get('title') or result.get('title')}\n"
        f"URL: {fetched.get('url') or result.get('url')}\n\n"
        f"Content:\n{content[:8000]}"
    )
    parsed = _extract_json_object(await _query_clerk_model(prompt, "You are the Clerk. Return only compact JSON source notes.")) or {}
    return {
        "id": f"S{len(session.sources) + 1}",
        "title": fetched.get("title") or result.get("title") or "Untitled",
        "url": fetched.get("url") or result.get("url"),
        "normalized_url": result.get("normalized_url") or _normalize_url(fetched.get("url") or result.get("url")),
        "domain": result.get("domain") or _source_domain(fetched.get("url") or result.get("url")),
        "snippet": result.get("snippet", ""),
        "fetch_success": bool(fetched.get("success")),
        "fetch_error": fetched.get("error", ""),
        "content": content[:10000],
        "summary": str(parsed.get("summary") or content[:700] or fetched.get("error") or "No summary available.").strip(),
        "key_claims": parsed.get("key_claims") if isinstance(parsed.get("key_claims"), list) else [],
        "perspective": str(parsed.get("perspective") or "Unlabeled"),
        "reliability_label": str(parsed.get("reliability_label") or "Unlabeled"),
        "relevance_note": str(parsed.get("relevance_note") or "").strip(),
    }


def _recent_search_query_text(session: DebateSession, limit: int = 12) -> str:
    if not session.search_queries:
        return "None yet."
    recent = session.search_queries[-limit:]
    return "\n".join(
        f"- {item.get('requester')} [{item.get('phase')}]: {item.get('query')}"
        for item in recent
    )


async def _refine_side_search_queries(
    session: DebateSession,
    side: str,
    phase: str,
    research_need: str,
    max_queries: int,
) -> list[str]:
    prompt = (
        "You are preparing research search terms for your debate position. Return strict JSON with key queries. "
        "Generate precise search-engine queries that will find sources directly relevant to your assigned position and the specific research need. "
        "Use concrete policy terms, comparison terms, likely source types, and disambiguating nouns. Avoid vague one-word searches. "
        "Do not repeat prior queries.\n\n"
        f"Topic: {session.topic}\n"
        f"Your side: {_side_label(session, side)}\n"
        f"Your position: {session.positions.get(side)}\n"
        f"Phase: {phase}\n"
        f"Research need: {research_need}\n\n"
        f"Prior search queries:\n{_recent_search_query_text(session)}\n\n"
        f"Return {max_queries} or fewer queries."
    )
    parsed = _extract_json_object(await _query_main_model(prompt, f"You are {_advocate_name(session, side)}, refining search terms for your debate side. Return only JSON.")) or {}
    raw_queries = parsed.get("queries") if isinstance(parsed.get("queries"), list) else []
    queries = [str(query).strip() for query in raw_queries if str(query).strip()]
    if not queries:
        queries = [f"{session.topic} {session.positions.get(side)} {research_need}".strip()]
    return queries[:max_queries]


async def _plan_clerk_queries(
    session: DebateSession,
    requester: str,
    phase: str,
    question: str,
    limit: int,
    seed_queries: list[str] | None = None,
) -> list[str]:
    side_context = ""
    if requester in {"pro", "anti"}:
        side_context = (
            f"\nRequesting side: {_side_label(session, requester)}"
            f"\nPosition represented: {session.positions.get(requester)}"
        )
    prompt = (
        "Generate precise web search queries for the Clerk in a formal debate. Return strict JSON with key queries. "
        "Use keyword-style queries, not natural-language questions. "
        "Queries must be directly relevant to the debate topic, the requesting side's position when present, and the specific research question. "
        "Prefer sources likely to contain evidence, data, primary claims, expert analysis, or reputable summaries. "
        "Do not drift into adjacent topics, generic definitions, or unrelated news.\n\n"
        f"Topic: {session.topic}\nRequester: {requester}{side_context}\nPhase: {phase}\nQuestion: {question}\n"
        f"Side-refined seed queries: {json.dumps(seed_queries or [], ensure_ascii=True)}\n"
        f"Prior search queries:\n{_recent_search_query_text(session)}\n"
        f"Max queries: {min(4, max(1, limit))}"
    )
    parsed = _extract_json_object(await _query_clerk_model(prompt, "You generate compact debate research queries. Return only JSON.")) or {}
    raw_queries = parsed.get("queries") if isinstance(parsed.get("queries"), list) else []
    queries = [str(query).strip() for query in (seed_queries or []) if str(query).strip()]
    queries.extend(str(query).strip() for query in raw_queries if str(query).strip())
    deduped = []
    seen = set()
    for query in queries:
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)
    queries = deduped
    if not queries:
        queries = [f"{session.topic} {question}".strip()]
    return queries[: min(4, max(1, limit))]


async def _filter_relevant_search_results(
    session: DebateSession,
    requester: str,
    phase: str,
    question: str,
    results: list[dict],
    max_results: int,
) -> list[dict]:
    if not results:
        return []
    side_context = ""
    if requester in {"pro", "anti"}:
        side_context = (
            f"\nRequesting side: {_side_label(session, requester)}"
            f"\nPosition represented: {session.positions.get(requester)}"
        )
    candidates = [
        {
            "index": index,
            "title": result.get("title"),
            "url": result.get("url"),
            "domain": result.get("domain"),
            "snippet": result.get("snippet"),
            "query": result.get("query"),
        }
        for index, result in enumerate(results[:24])
    ]
    prompt = (
        "Select only search results that are relevant enough to fetch for a formal debate research packet. "
        "Return strict JSON with key selected_indexes, an array of integer indexes. "
        "A result is relevant only if its title/snippet/domain indicate it can help answer the research need for this debate topic and side. "
        "Reject generic, tangential, unrelated, stale-looking, or clickbait results. "
        "If fewer than the requested maximum are relevant, return fewer. If none are clearly relevant, return an empty array.\n\n"
        f"Topic: {session.topic}\nRequester: {requester}{side_context}\nPhase: {phase}\nResearch need: {question}\n"
        f"Maximum selected results: {max_results}\n\n"
        f"Candidates JSON:\n{json.dumps(candidates, ensure_ascii=True)[:12000]}"
    )
    parsed = _extract_json_object(await _query_clerk_model(prompt, "You are the Clerk. Filter debate search results for relevance. Return only JSON."))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("selected_indexes"), list):
        return results[:max_results]

    selected = parsed.get("selected_indexes")
    selected_indexes = []
    for raw in selected:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(results) and index not in selected_indexes:
            selected_indexes.append(index)
        if len(selected_indexes) >= max_results:
            break
    if selected_indexes:
        return [results[index] for index in selected_indexes]
    return []


async def _clerk_research(
    session: DebateSession,
    requester: str,
    phase: str,
    question: str,
    max_sources: int,
    bot=None,
    seed_queries: list[str] | None = None,
) -> dict:
    max_sources = max(0, int(max_sources))
    queries = await _plan_clerk_queries(session, requester, phase, question, max_sources or 1, seed_queries=seed_queries)
    seen = {source.get("normalized_url") for source in session.sources if source.get("normalized_url")}
    candidates = []
    candidate_limit = max(max_sources * 4, max_sources)

    for query in queries:
        if len(candidates) >= candidate_limit:
            break
        session.search_queries.append({
            "requester": requester,
            "phase": phase,
            "query": query,
            "created_at": _now_label(),
        })
        results = await _search_web(query)
        for result in results:
            normalized = result.get("normalized_url")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(result)
            if len(candidates) >= candidate_limit:
                break

    targets = await _filter_relevant_search_results(
        session,
        requester,
        phase,
        question,
        candidates,
        max_sources,
    )

    new_sources = []
    for target in targets[:max_sources]:
        fetched = await _fetch_source_content(target)
        source = await _summarize_source(session, target, fetched)
        session.sources.append(source)
        new_sources.append(source)
        await _send_clerk_source_notice(bot, session, source)

    packet_summary = await _summarize_research_packet(session, requester, phase, question, new_sources)
    packet = {
        "id": f"P{len(session.research_packets) + 1}",
        "requester": requester,
        "phase": phase,
        "question": question,
        "queries": queries,
        "candidate_count": len(candidates),
        "source_ids": [source["id"] for source in new_sources],
        "summary": packet_summary,
        "created_at": _now_label(),
        "source_limit": max_sources,
    }
    session.research_packets.append(packet)
    session.touch()
    return packet


async def _summarize_research_packet(session: DebateSession, requester: str, phase: str, question: str, sources: list[dict]) -> str:
    if not sources:
        return "The Clerk found no usable new sources for this request."
    source_notes = "\n\n".join(
        f"{source['id']}. {source.get('title')}\nURL: {source.get('url')}\nSummary: {source.get('summary')}\nClaims: {'; '.join(str(claim) for claim in source.get('key_claims', [])[:4])}"
        for source in sources
    )
    prompt = (
        "Summarize this Clerk research packet for the requesting debate role. Cite source IDs. "
        "Return concise prose, not JSON.\n\n"
        f"Topic: {session.topic}\nRequester: {requester}\nPhase: {phase}\nQuestion: {question}\n\nSources:\n{source_notes}"
    )
    summary = await _query_clerk_model(prompt, "You are the Clerk. Summarize only the evidence gathered.")
    return summary.strip() or source_notes[:1000]


def _research_packets_for_side(session: DebateSession, side: str) -> list[dict]:
    return [packet for packet in session.research_packets if packet.get("requester") == side]


def _formal_transcript(session: DebateSession) -> str:
    if not session.formal_turns:
        return "No formal debate turns yet."
    lines = []
    for turn in session.formal_turns:
        round_label = f"Round {turn.get('round')}: " if turn.get("round") else ""
        speaker = turn.get("speaker")
        speaker_label = _side_label(session, speaker) if speaker in {"pro", "anti"} else str(speaker)
        lines.append(f"{round_label}{speaker_label} ({turn.get('kind')}): {turn.get('content')}")
    return "\n\n".join(lines)


def _packet_digest(packets: list[dict]) -> str:
    if not packets:
        return "No private research packets."
    return "\n\n".join(
        f"{packet.get('id')} [{packet.get('phase')}]\nQuestion: {packet.get('question')}\nSources: {', '.join(packet.get('source_ids') or [])}\nSummary: {packet.get('summary')}"
        for packet in packets
    )


def _build_moderator_context(session: DebateSession) -> str:
    return (
        f"Topic: {session.topic}\n"
        f"{_side_label(session, 'pro')} position: {session.positions.get('pro')}\n"
        f"{_side_label(session, 'anti')} position: {session.positions.get('anti')}\n\n"
        "Formal transcript only:\n"
        f"{_formal_transcript(session)}\n\n"
        f"Round results: {json.dumps(session.round_results, ensure_ascii=True)}"
    )


def _build_side_context(session: DebateSession, side: str) -> str:
    position = session.positions.get(side, "")
    side_label = _side_label(session, side)
    return (
        f"Topic: {session.topic}\n"
        f"Your side: {side_label}\n"
        f"Your position: {position}\n"
        f"Your private role brief: {session.role_briefs.get(side, '')}\n\n"
        "Your private research packets:\n"
        f"{_packet_digest(_research_packets_for_side(session, side))}\n\n"
        "Formal transcript visible to all roles:\n"
        f"{_formal_transcript(session)}"
    )


def _record_formal_turn(session: DebateSession, speaker: str, kind: str, content: str, *, round_number: int | None = None, source_ids: list[str] | None = None) -> None:
    session.formal_turns.append({
        "id": f"T{len(session.formal_turns) + 1}",
        "round": round_number,
        "speaker": speaker,
        "kind": kind,
        "content": str(content or "").strip(),
        "source_ids": source_ids or [],
        "created_at": _now_label(),
    })
    session.touch()


async def _define_positions(session: DebateSession, bot=None) -> None:
    packet = await _clerk_research(
        session,
        requester="moderator",
        phase="initial_positions",
        question=f"Identify the major opposing positions in this debate topic: {session.topic}",
        max_sources=INITIAL_POSITION_SOURCE_LIMIT,
        bot=bot,
    )
    prompt = (
        "You are the neutral Moderator. Define two debate positions for the user to approve. "
        "Return strict JSON with keys: pro_name, anti_name, pro_advocate_name, anti_advocate_name, pro, anti, framing. "
        "pro_name and anti_name must be short, concise labels that clearly identify each position, not generic labels like Pro-side or Anti-side. "
        "pro_advocate_name and anti_advocate_name must be fun short human first names for the debaters. "
        "The positions must be opposed and debate-ready.\n\n"
        f"Topic: {session.topic}\n\nClerk packet:\n{packet.get('summary')}"
    )
    parsed = _extract_json_object(await _query_main_model(prompt, "You are a neutral debate Moderator. Return only JSON.")) or {}
    session.side_names = {
        "pro": _clean_side_name(parsed.get("pro_name"), "Affirmative"),
        "anti": _clean_side_name(parsed.get("anti_name"), "Opposition"),
    }
    session.advocate_names = {
        "pro": _clean_side_name(parsed.get("pro_advocate_name"), "Jill"),
        "anti": _clean_side_name(parsed.get("anti_advocate_name"), "Mathias"),
    }
    session.positions = {
        "pro": str(parsed.get("pro") or f"In favor of {session.topic}").strip(),
        "anti": str(parsed.get("anti") or f"Opposed to {session.topic}").strip(),
    }
    session.position_framing = str(parsed.get("framing") or "").strip()
    session.status = "defining_positions"
    session.touch()


async def _revise_positions(session: DebateSession, instruction: str) -> None:
    prompt = (
        "Revise the debate positions according to the user's instruction. Return strict JSON with keys: "
        "pro_name, anti_name, pro_advocate_name, anti_advocate_name, pro, anti, framing. "
        "pro_name and anti_name must be short, concise labels that clearly identify each position. "
        "pro_advocate_name and anti_advocate_name must be fun short human first names for the debaters.\n\n"
        f"Topic: {session.topic}\n"
        f"Current {_side_label(session, 'pro')}: {session.positions.get('pro')}\n"
        f"Current {_side_label(session, 'anti')}: {session.positions.get('anti')}\n"
        f"User instruction: {instruction}"
    )
    parsed = _extract_json_object(await _query_main_model(prompt, "You are a neutral debate Moderator. Return only JSON.")) or {}
    session.side_names = {
        "pro": _clean_side_name(parsed.get("pro_name"), _side_name(session, "pro")),
        "anti": _clean_side_name(parsed.get("anti_name"), _side_name(session, "anti")),
    }
    session.advocate_names = {
        "pro": _clean_side_name(parsed.get("pro_advocate_name"), _advocate_name(session, "pro")),
        "anti": _clean_side_name(parsed.get("anti_advocate_name"), _advocate_name(session, "anti")),
    }
    if parsed.get("pro"):
        session.positions["pro"] = str(parsed.get("pro")).strip()
    if parsed.get("anti"):
        session.positions["anti"] = str(parsed.get("anti")).strip()
    if parsed.get("framing"):
        session.position_framing = str(parsed.get("framing")).strip()
    session.user_inputs.append({"time": _now_label(), "text": instruction, "state": session.status})
    session.touch()


async def _generate_side_questions(session: DebateSession, side: str, light_packet: dict) -> list[str]:
    prompt = (
        "Generate 3-6 research questions this debate side should answer before forming its thesis. "
        "Return strict JSON with key questions.\n\n"
        f"{_build_side_context(session, side)}\n\nLight research packet:\n{light_packet.get('summary')}"
    )
    parsed = _extract_json_object(await _query_main_model(prompt, f"You are the {side} debate role. Return only JSON.")) or {}
    raw_questions = parsed.get("questions") if isinstance(parsed.get("questions"), list) else []
    questions = [str(question).strip() for question in raw_questions if str(question).strip()]
    if not questions:
        questions = [
            f"What is the strongest evidence for {session.positions.get(side)}?",
            f"What objections to {session.positions.get(side)} must be answered?",
            f"What tradeoffs matter most for {session.topic}?",
        ]
    return questions[:6]


async def _prepare_side(bot, session: DebateSession, side: str) -> str:
    position = session.positions.get(side, "")
    light_queries = await _refine_side_search_queries(
        session,
        side,
        "side_light",
        f"Find initial evidence for this position: {position}",
        max_queries=3,
    )
    light_packet = await _clerk_research(
        session,
        requester=side,
        phase="side_light",
        question=f"Find initial evidence for this position: {position}",
        max_sources=SIDE_LIGHT_SOURCE_LIMIT,
        bot=bot,
        seed_queries=light_queries,
    )
    questions = await _generate_side_questions(session, side, light_packet)
    deep_research_need = "; ".join(questions)
    deep_queries = await _refine_side_search_queries(
        session,
        side,
        "side_deep",
        deep_research_need,
        max_queries=4,
    )
    deep_packet = await _clerk_research(
        session,
        requester=side,
        phase="side_deep",
        question=deep_research_need,
        max_sources=SIDE_DEEP_SOURCE_LIMIT,
        bot=bot,
        seed_queries=deep_queries,
    )
    prompt = (
        "Build this side's private debate brief. Return concise prose with thesis, best evidence, likely objections, and response strategy. "
        "Do not include hidden reasoning.\n\n"
        f"{_build_side_context(session, side)}\n\nDeep packet:\n{deep_packet.get('summary')}"
    )
    session.role_briefs[side] = await _query_main_model(prompt, f"You are the {side} side in a formal debate.")
    thesis_prompt = (
        "Write this side's opening thesis for the formal transcript. Use only claims supportable by your private research. "
        "Cite source IDs where useful. Return polished prose.\n\n"
        f"{_build_side_context(session, side)}"
    )
    thesis = await _query_main_model(thesis_prompt, f"You are the {side} side in a formal debate.")
    _record_formal_turn(session, side, "opening_thesis", thesis or session.role_briefs.get(side, ""))
    return thesis or session.role_briefs.get(side, "")


async def _moderator_questions(session: DebateSession) -> list[str]:
    prompt = (
        "Create 3-5 distinct formal debate round questions based only on the positions and opening theses. "
        "Return strict JSON with key questions. Do not use private research packets. "
        "Every question must be neutral and facilitate discussion between both positions. "
        "Do not favor either side, do not frame a question as a trap for one side, and do not ask a question only to one side. "
        "Each question should invite both sides to answer from their positions and respond to each other.\n\n"
        f"{_build_moderator_context(session)}"
    )
    parsed = _extract_json_object(await _query_main_model(prompt, "You are the neutral Moderator. Return only JSON.")) or {}
    raw_questions = parsed.get("questions") if isinstance(parsed.get("questions"), list) else []
    questions = [str(question).strip() for question in raw_questions if str(question).strip()]
    if not questions:
        questions = [
            f"What criteria should determine the strongest case on both sides of {session.topic}?",
            f"Which tradeoffs matter most when comparing both positions on {session.topic}?",
            f"How should the evidence offered by both positions be weighed?",
        ]
    return questions[:MAX_DEBATE_ROUNDS] if len(questions) >= MIN_DEBATE_ROUNDS else (questions + [
        "How should implementation risks be compared across both positions?",
        "How should second-order consequences be compared across both positions?",
    ])[:MIN_DEBATE_ROUNDS]


async def _round_side_turn(bot, session: DebateSession, side: str, kind: str, question: str, round_number: int, opponent_text: str = "") -> str:
    if kind == "answer":
        round_queries = await _refine_side_search_queries(
            session,
            side,
            f"round_{round_number}",
            f"{question}\nPosition: {session.positions.get(side)}",
            max_queries=2,
        )
        await _clerk_research(
            session,
            requester=side,
            phase=f"round_{round_number}",
            question=f"{question}\nPosition: {session.positions.get(side)}",
            max_sources=ROUND_SOURCE_LIMIT,
            bot=bot,
            seed_queries=round_queries,
        )
    prompt = (
        f"Write the {kind} for this debate round. Stay in role. Cite source IDs only if they appear in your private packets. "
        "Return only the formal debate turn.\n\n"
        f"Round question: {question}\n"
        f"Opponent text to address: {opponent_text or 'None yet.'}\n\n"
        f"{_build_side_context(session, side)}"
    )
    return await _query_main_model(prompt, f"You are the {side} side in a formal debate.")


async def _judge_round(session: DebateSession, round_number: int, question: str) -> dict:
    prompt = (
        "Judge this debate round based only on the formal transcript. Return strict JSON with keys: winner, rationale. "
        "winner must be pro, anti, or tie.\n\n"
        f"Round number: {round_number}\nRound question: {question}\n\n{_build_moderator_context(session)}"
    )
    parsed = _extract_json_object(await _query_main_model(prompt, "You are the neutral Moderator. Return only JSON.")) or {}
    winner = str(parsed.get("winner") or "tie").strip().lower()
    if winner not in {"pro", "anti", "tie"}:
        winner = "tie"
    result = {
        "round": round_number,
        "question": question,
        "winner": winner,
        "rationale": str(parsed.get("rationale") or "The Moderator did not return a rationale.").strip(),
    }
    session.round_results.append(result)
    return result


async def _run_question_round(bot, session: DebateSession, round_number: int, question: str) -> None:
    first, second = ("pro", "anti") if round_number % 2 == 1 else ("anti", "pro")
    first_answer = await _round_side_turn(bot, session, first, "answer", question, round_number)
    _record_formal_turn(session, first, "answer", first_answer, round_number=round_number)
    second_answer = await _round_side_turn(bot, session, second, "answer", question, round_number)
    _record_formal_turn(session, second, "answer", second_answer, round_number=round_number)
    first_response = await _round_side_turn(bot, session, first, "response", question, round_number, opponent_text=second_answer)
    _record_formal_turn(session, first, "response", first_response, round_number=round_number)
    second_response = await _round_side_turn(bot, session, second, "response", question, round_number, opponent_text=first_answer)
    _record_formal_turn(session, second, "response", second_response, round_number=round_number)
    await _judge_round(session, round_number, question)
    pro_full_response = "\n\n".join([
        "Initial answer:",
        first_answer if first == "pro" else second_answer,
        "Reply to opposing side:",
        first_response if first == "pro" else second_response,
    ])
    anti_full_response = "\n\n".join([
        "Initial answer:",
        first_answer if first == "anti" else second_answer,
        "Reply to opposing side:",
        first_response if first == "anti" else second_response,
    ])
    await _send_html_text(
        bot,
        session,
        await _round_responses_html(
            session,
            round_number,
            question,
            pro_full_response,
            anti_full_response,
        ),
    )


async def _final_side_thesis(session: DebateSession, side: str) -> None:
    prompt = (
        "Write this side's final thesis after all rounds. Address the formal debate record and preserve your position. "
        "Return only the formal final thesis.\n\n"
        f"{_build_side_context(session, side)}"
    )
    thesis = await _query_main_model(prompt, f"You are the {side} side in a formal debate.")
    _record_formal_turn(session, side, "final_thesis", thesis, round_number=len(session.debate_questions) + 1)


async def _build_final_memo(session: DebateSession) -> str:
    pro_wins = sum(1 for result in session.round_results if result.get("winner") == "pro")
    anti_wins = sum(1 for result in session.round_results if result.get("winner") == "anti")
    tie_count = sum(1 for result in session.round_results if result.get("winner") == "tie")
    score_line = f"Score: {_side_name(session, 'pro')} {pro_wins}, {_side_name(session, 'anti')} {anti_wins}, Ties {tie_count}"
    prompt = (
        "Write the final debate decision memo. Include: positions, round winners, strongest arguments, strongest objections, "
        "winner by round count, nuanced synthesized verdict that accounts for the losing side, confidence, and practical next step. "
        "Use concise Telegram-friendly Markdown.\n\n"
        f"{score_line}\n\n{_build_moderator_context(session)}"
    )
    memo = await _query_main_model(prompt, "You are the neutral Moderator writing the final debate decision memo.")
    return memo.strip() or f"Debate complete.\n\n{score_line}"


def _build_source_appendix(session: DebateSession) -> str:
    if not session.sources:
        return "No sources were gathered."
    lines = ["Sources"]
    for source in session.sources:
        status = "" if source.get("fetch_success") else " (fetch failed)"
        lines.append(
            f"[{source.get('id')}] {source.get('title')}{status}\n"
            f"{source.get('url')}\n"
            f"{source.get('summary')}"
        )
    return "\n\n".join(lines)


async def _send_status(bot, session: DebateSession, text: str, *, reply_markup=None, parse_mode: str | None = None):
    kwargs = {
        "chat_id": session.chat_id,
        "text": text,
        "reply_markup": reply_markup,
    }
    if session.message_thread_id is not None:
        kwargs["message_thread_id"] = session.message_thread_id
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    return await bot.send_message(**kwargs)


async def _send_long_text(bot, session: DebateSession, text: str) -> None:
    clean = str(text or "").strip()
    if not clean:
        return
    chunk_size = 3800
    for start in range(0, len(clean), chunk_size):
        await _send_status(bot, session, clean[start:start + chunk_size])


async def _send_html_text(bot, session: DebateSession, html_text: str) -> None:
    if not str(html_text or "").strip():
        return
    await send_split_html_message(
        bot,
        session.chat_id,
        html_text,
        message_thread_id=session.message_thread_id,
    )


async def _send_clerk_source_notice(bot, session: DebateSession, source: dict) -> None:
    if bot is None:
        return
    domain = source.get("domain") or _source_domain(source.get("url")) or "source"
    await _send_status(bot, session, f"The Clerk found a source!\n{domain}")


def _question_rounds_text(session: DebateSession) -> str:
    lines = ["👩🏼‍⚖️ Moderator has set the formal question rounds!", ""]
    for index, question in enumerate(session.debate_questions, start=1):
        lines.extend([f"Round {index}:", question, ""])
    return "\n".join(lines).strip()


async def _summarize_formal_response(session: DebateSession, side: str, label: str, text: str) -> str:
    prompt = (
        "Summarize this formal debate response for Telegram readers. "
        "Return 2-4 concise sentences or bullets. Do not judge the argument; only summarize what this side said.\n\n"
        f"Topic: {session.topic}\n"
        f"Side: {_side_label(session, side)}\n"
        f"Section: {label}\n\n"
        f"Formal response:\n{text[:9000]}"
    )
    summary = await _query_clerk_model(prompt, "You are the Clerk. Summarize formal debate responses without taking sides.")
    clean = re.sub(r"\s+", " ", str(summary or "")).strip()
    return clean or _short_visible_summary(text)


def _short_visible_summary(text: str, limit: int = 500) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _collapsed_argument_section(title: str, full_text: str) -> str:
    escaped_title = _telegram_escape(title)
    escaped_text = _telegram_escape(full_text)
    return f"<b>{escaped_title}</b>\n<blockquote expandable>{escaped_text}</blockquote>"


def _visible_summary_section(session: DebateSession, side: str, heading: str, summary: str, full_text: str) -> str:
    icon = "🔴" if side == "pro" else "🔵"
    title = f"{icon} {_side_name(session, side)} {heading}"
    return "\n\n".join([
        f"<b>{_telegram_escape(title)}:</b>",
        _telegram_escape(summary),
        _collapsed_argument_section("Full argument - expand to read", full_text),
    ])


async def _opening_statements_html(session: DebateSession, pro_opening: str, anti_opening: str) -> str:
    pro_summary = await _summarize_formal_response(session, "pro", "opening statement", pro_opening)
    anti_summary = await _summarize_formal_response(session, "anti", "opening statement", anti_opening)
    return "\n\n".join([
        "<b>The debate has begun!</b>",
        "<b>Opening Statements:</b>",
        _visible_summary_section(session, "pro", "Opening Statement", pro_summary, pro_opening),
        _visible_summary_section(session, "anti", "Opening Statement", anti_summary, anti_opening),
    ])


async def _round_responses_html(
    session: DebateSession,
    round_number: int,
    question: str,
    pro_response: str,
    anti_response: str,
) -> str:
    pro_summary = await _summarize_formal_response(session, "pro", f"question round {round_number}", pro_response)
    anti_summary = await _summarize_formal_response(session, "anti", f"question round {round_number}", anti_response)
    return "\n\n".join([
        f"<b>Question Round {round_number}:</b>",
        _telegram_escape(question),
        _visible_summary_section(session, "pro", "Response", pro_summary, pro_response),
        _visible_summary_section(session, "anti", "Response", anti_summary, anti_response),
    ])


def _callback(session_id: str, action: str, *parts: str) -> str:
    return ":".join([CALLBACK_PREFIX, session_id, action, *[str(part) for part in parts]])


def _position_markup(session: DebateSession) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Begin Debate", callback_data=_callback(session.id, "begin")),
            InlineKeyboardButton("Cancel", callback_data=_callback(session.id, "cancel")),
        ],
    ])


def _archive_action_markup(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Open debate", callback_data=_callback("archive", "open", session_id))],
    ])


def _position_text(session: DebateSession) -> str:
    lines = [
        "👩🏼‍⚖️ <b>Moderator has called a debate!</b>",
        "",
        _telegram_escape(session.topic),
        "",
        "<b>Positions:</b>",
        f"🔴 {_telegram_escape(_side_name(session, 'pro'))}, argued by {_telegram_escape(_advocate_name(session, 'pro'))}",
        f"🔵 {_telegram_escape(_side_name(session, 'anti'))}, argued by {_telegram_escape(_advocate_name(session, 'anti'))}",
    ]
    if session.position_framing:
        lines.extend(["", _telegram_escape(session.position_framing)])
    lines.extend(["", "▶️ Tap <b>Begin Debate</b> or reply with revisions."])
    return "\n".join(lines)


async def _send_position_prompt(bot, session: DebateSession) -> None:
    await _send_status(bot, session, _position_text(session), reply_markup=_position_markup(session), parse_mode="HTML")


async def _run_position_definition(session: DebateSession, bot) -> None:
    try:
        ACTIVE_DEBATES[session.key()] = session
        await _define_positions(session)
        await _send_position_prompt(bot, session)
    except Exception as exc:
        session.status = "error"
        logging.error("DEBATE: Position definition failed for %s: %s", session.id, exc, exc_info=True)
        await _send_status(bot, session, f"⚠️ Debate setup failed: {exc}")


async def _run_debate_session(session: DebateSession, bot) -> None:
    current_task = asyncio.current_task()
    globals.register_foreground_loop(
        session.id,
        loop_type="debate",
        chat_id=session.chat_id,
        message_thread_id=session.message_thread_id,
    )
    try:
        ACTIVE_DEBATES[session.key()] = session
        session.status = "running"
        session.touch()
        await _send_status(bot, session, f"🔴 {_advocate_name(session, 'pro')} is researching in preparation for the debate.")
        pro_opening = await _prepare_side(bot, session, "pro")
        await _send_status(bot, session, f"🔵 {_advocate_name(session, 'anti')} is researching in preparation for the debate.")
        anti_opening = await _prepare_side(bot, session, "anti")
        session.debate_questions = await _moderator_questions(session)
        await _send_status(bot, session, _question_rounds_text(session))
        await _send_html_text(bot, session, await _opening_statements_html(session, pro_opening, anti_opening))
        for index, question in enumerate(session.debate_questions, start=1):
            await _run_question_round(bot, session, index, question)
        await _final_side_thesis(session, "pro")
        await _final_side_thesis(session, "anti")
        session.final_memo = await _build_final_memo(session)
        session.source_appendix = _build_source_appendix(session)
        session.status = "completed"
        _archive_session(session)
        await _send_long_text(bot, session, session.final_memo)
        await _send_long_text(bot, session, session.source_appendix)
        ACTIVE_DEBATES.pop(session.key(), None)
    except asyncio.CancelledError:
        session.status = "cancelled"
        raise
    except Exception as exc:
        session.status = "error"
        logging.error("DEBATE: Session %s failed: %s", session.id, exc, exc_info=True)
        await _send_status(bot, session, f"⚠️ Debate failed: {exc}")
    finally:
        globals.unregister_foreground_loop(session.id)
        if DEBATE_TASKS.get(session.id) is current_task:
            DEBATE_TASKS.pop(session.id, None)


def _start_debate_task(session: DebateSession, bot) -> None:
    existing = DEBATE_TASKS.get(session.id)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_run_debate_session(session, bot))
    DEBATE_TASKS[session.id] = task


def _active_session_by_id(session_id: str) -> DebateSession | None:
    for session in ACTIVE_DEBATES.values():
        if session.id == session_id:
            return session
    return None


def _active_session_for_update(update) -> DebateSession | None:
    if not update.effective_chat:
        return None
    thread_id = getattr(getattr(update, "message", None), "message_thread_id", None)
    if thread_id is None and getattr(update, "callback_query", None) and getattr(update.callback_query, "message", None):
        thread_id = getattr(update.callback_query.message, "message_thread_id", None)
    return ACTIVE_DEBATES.get(_session_key(update.effective_chat.id, thread_id))


async def _cancel_session_object(bot, session: DebateSession) -> None:
    task = DEBATE_TASKS.pop(session.id, None)
    if task and not task.done():
        task.cancel()
    session.status = "cancelled"
    ACTIVE_DEBATES.pop(session.key(), None)
    globals.unregister_foreground_loop(session.id)
    await _send_status(bot, session, "🛑 Debate cancelled.")


def _archive_root() -> Path:
    return Path(DEBATE_ARCHIVE_DIR).expanduser()


def _index_path() -> Path:
    return Path(DEBATE_INDEX_PATH).expanduser()


def _load_index() -> list[dict]:
    path = _index_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_index(entries: list[dict]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _archive_session(session: DebateSession) -> Path:
    root = _archive_root()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / f"{datetime.now(USER_TIMEZONE).strftime('%Y%m%d-%H%M%S')}-{_slugify(session.topic)}-{session.id}"
    folder.mkdir(parents=True, exist_ok=True)
    session.archive_path = str(folder)
    session.archive_label = session.archive_label or session.topic[:60]
    (folder / "session.json").write_text(json.dumps(session.to_dict(), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    (folder / "memo.md").write_text(session.final_memo, encoding="utf-8")
    (folder / "sources.md").write_text(session.source_appendix, encoding="utf-8")
    (folder / "transcript.md").write_text(_formal_transcript(session), encoding="utf-8")
    entries = [entry for entry in _load_index() if entry.get("id") != session.id]
    entries.insert(0, {
        "id": session.id,
        "topic": session.topic,
        "archive_label": session.archive_label,
        "created_at": session.created_at,
        "archived_at": _now_iso(),
        "archive_path": str(folder),
        "memo_path": str(folder / "memo.md"),
        "source_count": len(session.sources),
        "round_count": len(session.round_results),
    })
    _save_index(entries)
    return folder


def clear_debate_archives() -> dict:
    root = _archive_root()
    entries = _load_index()
    removed = 0
    if root.exists():
        for child in root.iterdir():
            if child.is_dir():
                for path in child.rglob("*"):
                    if path.is_file():
                        path.unlink()
                for path in sorted(child.rglob("*"), reverse=True):
                    if path.is_dir():
                        path.rmdir()
                child.rmdir()
                removed += 1
            elif child.is_file():
                child.unlink()
                removed += 1
    root.mkdir(parents=True, exist_ok=True)
    _save_index([])
    return {"archived_sessions": len(entries), "removed_items": removed, "archive_dir": str(root)}


async def _send_debate_help(update) -> None:
    await update.message.reply_text("\n".join([
        "⚖️ <b>Debate commands</b>",
        "",
        "/debate &lt;topic&gt; - Start a structured four-role debate with named sides.",
        "/debate status - Show the active debate status.",
        "/debate cancel - Cancel the active debate.",
        "/debate list - Show archived debates.",
        "/debate open &lt;id&gt; - Send an archived debate memo.",
        "/debate clear - Delete archived debates.",
    ]), parse_mode="HTML")


async def _send_debate_status(update, context) -> None:
    session = _active_session_for_update(update)
    if not session:
        await update.message.reply_text("No active debate in this chat/thread.")
        return
    await update.message.reply_text(
        f"Debate {session.id}: {session.status}\n"
        f"Topic: {session.topic}\n"
        f"Sources: {len(session.sources)}\n"
        f"Formal turns: {len(session.formal_turns)}\n"
        f"Rounds judged: {len(session.round_results)}"
    )


async def _send_debate_list(update, context) -> None:
    entries = _load_index()
    if not entries:
        await update.message.reply_text("No archived debates yet.")
        return
    lines = ["Archived debates:"]
    for entry in entries[:20]:
        lines.append(f"- {entry.get('id')}: {entry.get('archive_label') or entry.get('topic')} ({entry.get('round_count', 0)} rounds)")
    await update.message.reply_text("\n".join(lines))


async def _open_archived_debate(update, context, session_id: str) -> None:
    entry = next((item for item in _load_index() if item.get("id") == session_id), None)
    target_message = update.message or (update.callback_query.message if update.callback_query else None)
    if not target_message:
        return
    if not entry:
        await target_message.reply_text(f"No archived debate found for ID {session_id}.")
        return
    memo_path = Path(str(entry.get("memo_path") or ""))
    if not memo_path.exists():
        await target_message.reply_text(f"Archived debate {session_id} is missing its memo file.")
        return
    await target_message.reply_text(memo_path.read_text(encoding="utf-8")[:3800])


def _topic_has_enough_substance(topic: str) -> bool:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", str(topic or ""))
    return len(words) >= 2


async def handle_debate_command(update, context) -> None:
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(chat_id, update.message.message_thread_id)
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(thread_id)
    globals.current_user_id.set(update.effective_user.id if update.effective_user else None)

    args = list(getattr(context, "args", []) or [])
    if not args:
        await _send_debate_help(update)
        return

    subcommand = args[0].lower()
    if subcommand == "help":
        await _send_debate_help(update)
        return
    if subcommand == "status":
        await _send_debate_status(update, context)
        return
    if subcommand == "cancel":
        session = _active_session_for_update(update)
        if not session:
            await update.message.reply_text("No active debate in this chat/thread.")
            return
        await _cancel_session_object(context.bot, session)
        return
    if subcommand == "list":
        await _send_debate_list(update, context)
        return
    if subcommand == "clear":
        result = clear_debate_archives()
        await update.message.reply_text(
            f"🧹 Deleted archived debates.\nArchived sessions cleared: {result['archived_sessions']}\nArchive items removed: {result['removed_items']}"
        )
        return
    if subcommand == "open" and len(args) >= 2:
        await _open_archived_debate(update, context, args[1])
        return
    if subcommand == "open":
        await _send_debate_help(update)
        return

    key = _session_key(chat_id, thread_id)
    if key in ACTIVE_DEBATES and ACTIVE_DEBATES[key].status in {"preparing_positions", "defining_positions", "running"}:
        await update.message.reply_text("⚖️ A debate is already active here. Use /debate status or /debate cancel.")
        return

    topic = " ".join(args).strip()
    if not _topic_has_enough_substance(topic):
        await update.message.reply_text("⚖️ Give me a debate topic with enough substance, for example: /debate A progressive tax rate")
        return

    session = DebateSession(
        id=_short_id(),
        topic=topic,
        chat_id=chat_id,
        message_thread_id=thread_id,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    ACTIVE_DEBATES[key] = session
    await _run_position_definition(session, context.bot)


def _looks_like_acceptance(text: str) -> bool:
    clean = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return clean in {"yes", "y", "perfect", "begin", "begin debate", "start", "start debate", "get started", "looks good", "approved"} or "get started" in clean


async def handle_debate_message(update, context, content_text: str) -> bool:
    session = _active_session_for_update(update)
    if not session or session.status != "defining_positions" or not update.message:
        return False
    text = str(content_text or update.message.text or "").strip()
    if not text:
        return False
    if _looks_like_acceptance(text):
        _start_debate_task(session, context.bot)
        await update.message.reply_text("🎙️ Beginning the formal debate.")
        return True
    if any(word in text.lower() for word in ("cancel", "stop", "discard")):
        await _cancel_session_object(context.bot, session)
        return True
    await _revise_positions(session, text)
    await _send_position_prompt(context.bot, session)
    return True


async def handle_debate_callback(update, context) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return
    session_id = parts[1]
    action = parts[2]

    if session_id == "archive" and action == "open" and len(parts) >= 4:
        await _open_archived_debate(update, context, parts[3])
        return

    session = _active_session_by_id(session_id)
    if not session:
        await query.message.reply_text("That debate session is no longer active.")
        return
    if action == "cancel":
        await _cancel_session_object(context.bot, session)
        return
    if action == "begin":
        if session.status != "defining_positions":
            await query.message.reply_text(f"That debate is currently {session.status}.")
            return
        _start_debate_task(session, context.bot)
        await query.message.reply_text("🎙️ Beginning the formal debate.")
