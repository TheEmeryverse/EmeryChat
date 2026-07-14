import json
import logging
import math
import os
import re
import threading
import asyncio

from datetime import datetime, timedelta

from emery.config import (
    ENABLE_MEMORY, MEMORY_STORE_PATH, CAMERA_LOG_FILE_PATH,
    EMBEDDING_MODEL_ID, EMBEDDING_OLLAMA_URL, USER_NAME, USER_2_NAME,
    USER_LOCATION, USER_TIMEZONE, USER_BIRTHDAY, USER_FAMILY, USER_PROFESSION,
    SECONDARY_USER_ID, PRIMARY_USER_ID, USER_RELATIONSHIP,
    get_user_profile
)
import emery.globals as globals
from emery.logging_utils import safe_preview


STORE_VERSION = 3
MAX_MEMORY_RESULTS = 6
MAX_TOPIC_RESULTS = 3
MAX_GROUP_CONTEXT_RESULTS = 3
_store_lock = threading.RLock()
_is_consolidating = False
_topic_summary_chats = set()
_last_summary_hist_len = {}

_STOP_WORDS = {
    "i", "me", "my", "myself", "we", "our", "ours", "ourselves", "you", "your", "yours",
    "he", "him", "his", "she", "her", "hers", "it", "its", "they", "them", "their",
    "what", "which", "who", "whom", "this", "that", "these", "those", "am", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "having", "do", "does",
    "did", "doing", "a", "an", "the", "and", "but", "if", "or", "because", "as", "until",
    "while", "of", "at", "by", "for", "with", "about", "against", "between", "into",
    "through", "during", "before", "after", "above", "below", "to", "from", "up", "down",
    "in", "out", "on", "off", "over", "under", "again", "further", "then", "once", "here",
    "there", "when", "where", "why", "how", "all", "any", "both", "each", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own", "same", "so",
    "than", "too", "very", "s", "t", "can", "will", "just", "don", "should", "now",
    "please", "emery", "remember", "chat", "group", "today", "tomorrow", "yesterday"
}


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _tokenize(text: str) -> list[str]:
    clean = re.sub(r"[^\w\s]", " ", (text or "").lower())
    tokens = []
    for token in clean.split():
        if len(token) < 3 or token in _STOP_WORDS:
            continue
        if token.endswith("ies") and len(token) > 5:
            token = token[:-3] + "y"
        elif token.endswith("es") and len(token) > 4:
            token = token[:-2]
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        tokens.append(token)
    return tokens


def _extract_tags(text: str, limit: int = 6) -> list[str]:
    counts = {}
    for token in _tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    return [token for token, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _memory_store_path() -> str:
    return MEMORY_STORE_PATH


def _default_store() -> dict:
    return {
        "version": STORE_VERSION,
        "next_id": 1,
        "items": []
    }


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(temp_path, path)


def _repair_store_file(path: str, reason: str) -> dict:
    repaired = _default_store()
    try:
        if os.path.exists(path):
            backup_path = f"{path}.corrupt"
            if os.path.exists(backup_path):
                os.remove(backup_path)
            os.replace(path, backup_path)
            logging.warning("⚠️ MEMORY: Moved corrupt store to %s after %s", backup_path, reason)
        _atomic_write_json(path, repaired)
    except Exception as e:
        logging.error("❌ MEMORY: Failed repairing store %s: %s", path, e, exc_info=True)
    return repaired


def _build_item(
    store: dict,
    *,
    owner_user_id=None,
    source_user_id=None,
    source_chat_id=None,
    item_type: str = "fact",
    text: str,
    scope: str = "private",
    visibility: str = "dm_only",
    status: str = "active",
    source_type: str = "chat",
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
    metadata: dict | None = None,
) -> dict:
    item_id = f"mem_{store['next_id']}"
    store["next_id"] += 1
    now_iso = _utc_now_iso()
    return {
        "id": item_id,
        "owner_user_id": owner_user_id,
        "source_user_id": source_user_id,
        "source_chat_id": source_chat_id,
        "type": item_type,
        "text": (text or "").strip(),
        "scope": scope,
        "visibility": visibility,
        "status": status,
        "source_type": source_type,
        "tags": list(tags or _extract_tags(text)),
        "embedding": list(embedding or []),
        "metadata": dict(metadata or {}),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def _load_store_locked() -> dict:
    path = _memory_store_path()
    if not os.path.exists(path):
        store = _default_store()
        _atomic_write_json(path, store)
        return store

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data.setdefault("version", STORE_VERSION)
            data.setdefault("next_id", len(data["items"]) + 1)
            return data
        logging.error("❌ MEMORY: Invalid store structure in %s", path)
        return _repair_store_file(path, "invalid structure")
    except Exception as e:
        logging.error("❌ MEMORY: Failed loading store %s: %s", path, e, exc_info=True)
        return _repair_store_file(path, str(e))


def _save_store_locked(store: dict) -> None:
    store["version"] = STORE_VERSION
    path = _memory_store_path()
    _atomic_write_json(path, store)


def _topic_text_from_metadata(item: dict) -> str:
    metadata = item.get("metadata") or {}
    if item.get("type") != "topic" or not metadata:
        return item.get("text", "").strip()
    summary = (metadata.get("summary") or item.get("text") or "").strip()
    if not summary:
        return ""
    date_label = (metadata.get("date_label") or "").strip()
    channel_type = (metadata.get("channel_type") or "").strip()
    tags = metadata.get("tags") or item.get("tags", [])
    tag_str = ", ".join(tag for tag in tags if tag)[:160]
    prefix = "- "
    if date_label and channel_type:
        prefix += f"On {date_label} (in {channel_type}): "
    elif date_label:
        prefix += f"On {date_label}: "
    return prefix + summary + (f" [Tags: {tag_str}]" if tag_str else "")


def _default_profile_lines(user_id: int) -> list[str]:
    profile = get_user_profile(user_id)
    return [
        f"- Name: {profile['name']}",
        f"- Location: {USER_LOCATION}",
        f"- Timezone: {USER_TIMEZONE}",
        f"- Birthday: {profile['birthday']}",
        f"- Family: {profile['family']}",
        f"- Profession: {profile['profession']}",
    ]


def _resolve_embedding_url() -> str:
    url = (EMBEDDING_OLLAMA_URL or "").strip()
    if not url:
        return ""
    url = url.rstrip("/")
    if url.endswith(("/api/embed", "/api/embeddings", "/v1/embeddings")):
        return url
    if not url.endswith("/api"):
        url += "/api"
    return url + "/embed"


async def _get_text_embedding(text: str) -> list[float]:
    text = (text or "").strip()
    if not text:
        return []

    url = _resolve_embedding_url()
    if not url:
        return []

    is_openai_api = url.endswith("/v1/embeddings")
    payload = {"model": EMBEDDING_MODEL_ID, "input": text}
    if not is_openai_api:
        payload["keep_alive"] = -1
    try:
        response = await globals.http_client.post(url, json=payload, timeout=120)
        if response.status_code != 200 and not is_openai_api and url.endswith("/embed"):
            fallback_url = url[:-len("/embed")] + "/embeddings"
            response = await globals.http_client.post(
                fallback_url,
                json={"model": EMBEDDING_MODEL_ID, "prompt": text, "keep_alive": -1},
                timeout=120
            )
        if response.status_code != 200:
            logging.warning(
                "⚠️ EMBEDDINGS: API error %s from %s for %s",
                response.status_code,
                url,
                EMBEDDING_MODEL_ID,
            )
            return []
        data = response.json()
        if isinstance(data.get("embedding"), list):
            return [float(value) for value in data["embedding"]]
        embeddings = data.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            first = embeddings[0]
            if isinstance(first, list):
                return [float(value) for value in first]
        openai_data = data.get("data")
        if isinstance(openai_data, list) and openai_data:
            first = openai_data[0]
            if isinstance(first, dict) and isinstance(first.get("embedding"), list):
                return [float(value) for value in first["embedding"]]
        return []
    except Exception as e:
        logging.warning("⚠️ EMBEDDINGS: Failed querying %s: %s", EMBEDDING_MODEL_ID, e)
        return []


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(a * a for a in vec_a))
    mag_b = math.sqrt(sum(b * b for b in vec_b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _mentions_household(query: str) -> bool:
    clean = _normalize_text(query)
    aliases = {
        USER_2_NAME.lower().strip(),
        USER_RELATIONSHIP.lower().strip(),
        "spouse", "wife", "husband", "partner", "family", "household", "both of us", "together"
    }
    aliases.discard("")
    return any(alias and alias in clean for alias in aliases)


def _mentions_secondary_user(query: str) -> bool:
    clean = _normalize_text(query)
    aliases = {USER_2_NAME.lower().strip(), "spouse", "wife", "husband", "partner"}
    aliases.discard("")
    return any(alias and alias in clean for alias in aliases)


def _primary_known_names() -> set[str]:
    names = {USER_NAME.lower().strip()}
    if USER_2_NAME:
        names.add(USER_2_NAME.lower().strip())
    relationship = USER_RELATIONSHIP.lower().strip()
    if relationship:
        names.add(relationship)
    names.update({"spouse", "wife", "husband", "partner", "family"})
    return {name for name in names if name}


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _normalize_topic_tags(raw_tags, summary: str) -> list[str]:
    tags = []
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            clean = str(tag).strip().lower()
            if clean:
                tags.append(clean)
    tags = _dedupe_preserve_order(tags)
    if len(tags) < 2:
        fallback = [tag.lower() for tag in _extract_tags(summary, limit=5)]
        tags = _dedupe_preserve_order(tags + fallback)
    return tags[:5]


def _normalize_topic_participants(raw_participants, allowed_names: list[str]) -> list[str]:
    allowed_lookup = {name.lower(): name for name in allowed_names if name}
    participants = []
    if isinstance(raw_participants, list):
        for name in raw_participants:
            clean = str(name).strip()
            if not clean:
                continue
            canonical = allowed_lookup.get(clean.lower())
            if canonical:
                participants.append(canonical)
    return _dedupe_preserve_order(participants)


def _topic_signature(topic_payload: dict) -> tuple:
    summary_tokens = tuple(sorted(set(_tokenize(topic_payload.get("summary", "")))))
    tags = tuple(sorted(set(topic_payload.get("tags") or [])))
    return (
        topic_payload.get("topic_class", ""),
        summary_tokens[:8],
        tags[:5],
    )


def _normalize_topic_payloads(raw_topics, *, allowed_names: list[str], default_topic_class: str) -> list[dict]:
    normalized_topics = []
    seen_signatures = set()
    if not isinstance(raw_topics, list):
        return normalized_topics

    for raw_topic in raw_topics[:3]:
        if not isinstance(raw_topic, dict):
            continue
        summary = str(raw_topic.get("summary") or "").strip()
        if not summary:
            continue

        topic_class = str(raw_topic.get("topic_class") or default_topic_class).strip().lower()
        if topic_class not in {"private", "household", "group"}:
            topic_class = default_topic_class

        tags = _normalize_topic_tags(raw_topic.get("tags"), summary)
        if len(tags) < 2:
            continue

        participants = _normalize_topic_participants(raw_topic.get("participants"), allowed_names)

        confidence_raw = raw_topic.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))

        topic_payload = {
            "summary": summary,
            "topic_class": topic_class,
            "tags": tags,
            "participants": participants,
            "confidence": confidence,
        }
        signature = _topic_signature(topic_payload)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        normalized_topics.append(topic_payload)

    return normalized_topics


def _debug_topic_payloads(label: str, payload) -> None:
    try:
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    except Exception:
        serialized = str(payload)
    logging.info("🧭 TOPIC DEBUG [%s]: %s", label, safe_preview(serialized, max_len=800))


def _infer_scope_visibility(text: str, owner_user_id: int | None, chat_id: int | None, item_type: str) -> tuple[str, str]:
    normalized = _normalize_text(text)
    if item_type == "topic":
        if chat_id and chat_id < 0:
            return "group_context", "group_safe"
        return "private", "dm_only"
    if "camera" in normalized or "reolink" in normalized:
        return "system", "group_safe"
    household_markers = {
        USER_2_NAME.lower().strip(),
        USER_NAME.lower().strip(),
        "spouse", "wife", "husband", "partner", "family", "house", "home", "kids"
    }
    household_markers.discard("")
    if SECONDARY_USER_ID != 0 and any(marker in normalized for marker in household_markers):
        return "shared_household", "household"
    if chat_id and chat_id < 0:
        return "private", "dm_only"
    return "private", "dm_only"


def _find_duplicate_locked(store: dict, item: dict) -> dict | None:
    normalized = _normalize_text(item.get("text", ""))
    for existing in store.get("items", []):
        if existing.get("status") not in {"active", "raw"}:
            continue
        if existing.get("owner_user_id") != item.get("owner_user_id"):
            continue
        if existing.get("type") != item.get("type"):
            continue
        if existing.get("scope") != item.get("scope"):
            continue
        if item.get("type") == "topic":
            existing_meta = existing.get("metadata") or {}
            item_meta = item.get("metadata") or {}
            existing_class = existing_meta.get("topic_class", "")
            item_class = item_meta.get("topic_class", "")
            if existing_class != item_class:
                continue
            existing_tags = set(existing_meta.get("tags") or existing.get("tags", []))
            item_tags = set(item_meta.get("tags") or item.get("tags", []))
            if existing_tags and item_tags and existing_tags.isdisjoint(item_tags):
                continue
        if _normalize_text(existing.get("text", "")) == normalized:
            return existing
    return None


def _parse_iso_date(value: str) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.min


def _lexical_score(query_tokens: list[str], item: dict) -> float:
    if not query_tokens:
        return 0.0
    item_tokens = set(_tokenize(item.get("text", "") + " " + " ".join(item.get("tags", []))))
    if not item_tokens:
        return 0.0
    overlap = len(set(query_tokens) & item_tokens)
    return overlap / max(len(set(query_tokens)), 1)


def _candidate_items_for_query(store: dict, user_id: int, query: str, chat_id: int | None) -> tuple[list[dict], list[dict]]:
    items = [item for item in store.get("items", []) if item.get("status") == "active"]
    profile_items = []
    candidates = []
    is_group = bool(chat_id and chat_id < 0)
    household_relevant = _mentions_household(query)
    secondary_relevant = _mentions_secondary_user(query)

    for item in items:
        item_type = item.get("type")
        owner = item.get("owner_user_id")
        scope = item.get("scope")
        visibility = item.get("visibility")

        if item_type == "profile":
            if owner == user_id or (secondary_relevant and owner == SECONDARY_USER_ID):
                profile_items.append(item)
            continue

        if scope == "group_context":
            if chat_id and item.get("source_chat_id") == chat_id:
                candidates.append(item)
            continue

        if is_group:
            if visibility != "group_safe":
                continue
            candidates.append(item)
            continue

        if owner == user_id:
            candidates.append(item)
            continue

        if scope == "shared_household" and household_relevant:
            candidates.append(item)
            continue

        if secondary_relevant and owner == SECONDARY_USER_ID:
            candidates.append(item)

    return profile_items, candidates


async def retrieve_relevant_memories(user_query: str, user_id: int = None) -> str:
    if not ENABLE_MEMORY:
        return ""
    if user_id is None:
        user_id = globals.current_user_id.get()
    if user_id is None:
        return ""

    with _store_lock:
        store = _load_store_locked()
        profile_items, candidates = _candidate_items_for_query(
            store,
            user_id=user_id,
            query=user_query,
            chat_id=globals.TARGET_CHAT_ID.get(),
        )

    query_text = re.sub(r'^\[[^\]]+\]\s*', '', user_query or '')
    query_tokens = _tokenize(query_text)
    query_embedding = await _get_text_embedding(query_text)

    scored = []
    for item in candidates:
        lexical = _lexical_score(query_tokens, item)
        semantic = _cosine_similarity(query_embedding, item.get("embedding", []))
        freshness = 0.0
        updated = _parse_iso_date(item.get("updated_at"))
        if updated != datetime.min:
            age_days = max((datetime.utcnow() - updated).days, 0)
            freshness = max(0.0, 0.15 - min(age_days, 30) * 0.005)
        score = (semantic * 0.75) + (lexical * 0.35) + freshness
        if item.get("type") == "topic":
            score += 0.05
        if item.get("scope") == "group_context":
            score += 0.08
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda entry: (entry[0], _parse_iso_date(entry[1].get("updated_at"))), reverse=True)

    fact_lines = []
    topic_lines = []
    group_lines = []
    for _score, item in scored:
        line = _topic_text_from_metadata(item) if item.get("type") == "topic" else f"- {item.get('text', '').strip()}"
        if item.get("scope") == "group_context":
            if len(group_lines) < MAX_GROUP_CONTEXT_RESULTS and line not in group_lines:
                group_lines.append(line)
        elif item.get("type") == "topic":
            if len(topic_lines) < MAX_TOPIC_RESULTS and line not in topic_lines:
                topic_lines.append(line)
        else:
            if len(fact_lines) < MAX_MEMORY_RESULTS and line not in fact_lines:
                fact_lines.append(line)

    profile_lines = _default_profile_lines(user_id)
    seen_profile = {line.lower() for line in profile_lines}
    for item in sorted(profile_items, key=lambda entry: _parse_iso_date(entry.get("updated_at")), reverse=True):
        line = _topic_text_from_metadata(item) if item.get("type") == "topic" else f"- {item.get('text', '').strip()}"
        if line.lower() not in seen_profile:
            profile_lines.append(line)
            seen_profile.add(line.lower())

    sections = ["## User Profile & Preferences", *profile_lines]
    if fact_lines:
        sections.extend(["", "## Relevant Recalled Memories", *fact_lines])
    if topic_lines:
        sections.extend(["", "## Relevant Conversation Topics", *topic_lines])
    if group_lines:
        sections.extend(["", "## Group Context Memory", *group_lines])
    return "\n".join(sections)


def _store_item_locked(
    store: dict,
    *,
    owner_user_id=None,
    source_user_id=None,
    source_chat_id=None,
    item_type: str,
    text: str,
    scope: str,
    visibility: str,
    status: str = "active",
    source_type: str = "chat",
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> tuple[dict, bool]:
    item = _build_item(
        store,
        owner_user_id=owner_user_id,
        source_user_id=source_user_id,
        source_chat_id=source_chat_id,
        item_type=item_type,
        text=text,
        scope=scope,
        visibility=visibility,
        status=status,
        source_type=source_type,
        tags=tags,
        metadata=metadata,
    )
    duplicate = _find_duplicate_locked(store, item)
    if duplicate:
        duplicate["updated_at"] = _utc_now_iso()
        merged_tags = set(duplicate.get("tags", []))
        merged_tags.update(item.get("tags", []))
        duplicate["tags"] = sorted(merged_tags)
        duplicate_meta = duplicate.setdefault("metadata", {})
        item_meta = item.get("metadata") or {}
        if item_meta:
            duplicate_meta.update({k: v for k, v in item_meta.items() if v not in (None, "", [], {})})
        return duplicate, False
    store["items"].append(item)
    return item, True


async def save_user_memory(fact: str, user_id: int = None) -> str:
    if user_id is None:
        user_id = globals.current_user_id.get()
    if not ENABLE_MEMORY or user_id is None:
        return "Memory is disabled."

    fact = (fact or "").strip()
    if not fact:
        return "No memory content provided."

    chat_id = globals.TARGET_CHAT_ID.get()
    scope, visibility = _infer_scope_visibility(fact, user_id, chat_id, "fact")
    with _store_lock:
        store = _load_store_locked()
        item, created = _store_item_locked(
            store,
            owner_user_id=user_id,
            source_user_id=user_id,
            source_chat_id=chat_id,
            item_type="fact",
            text=fact,
            scope=scope,
            visibility=visibility,
            status="active",
            source_type="chat",
        )
        _save_store_locked(store)

    asyncio.create_task(consolidate_memory_background())
    action = "saved" if created else "updated"
    logging.debug("💾 MEMORY: %s memory for user %s: %s", action, user_id, safe_preview(fact, max_len=120))
    return f"Successfully {action} memory: '{fact}'"


async def consolidate_memory_background(user_id: int = None) -> None:
    global _is_consolidating
    if _is_consolidating or not ENABLE_MEMORY:
        return

    _is_consolidating = True
    try:
        with _store_lock:
            store = _load_store_locked()
            targets = []
            for item in store.get("items", []):
                if item.get("status") == "raw":
                    item["status"] = "active"
                    item["updated_at"] = _utc_now_iso()
                if item.get("status") == "active" and not item.get("embedding"):
                    targets.append({"id": item["id"], "text": item.get("text", "")})

        if targets:
            resolved_embeddings = {}
            for target in targets:
                embedding = await _get_text_embedding(target["text"])
                if embedding:
                    resolved_embeddings[target["id"]] = embedding

            with _store_lock:
                store = _load_store_locked()
                for item in store.get("items", []):
                    if item.get("id") in resolved_embeddings:
                        item["embedding"] = resolved_embeddings[item["id"]]
                        item["updated_at"] = _utc_now_iso()
                _save_store_locked(store)
        else:
            with _store_lock:
                store = _load_store_locked()
                _save_store_locked(store)

    except Exception as e:
        logging.error("❌ MEMORY: Consolidation crash: %s", e, exc_info=True)
    finally:
        _is_consolidating = False


def wipe_memory(user_id: int = None) -> bool:
    if user_id is None:
        user_id = globals.current_user_id.get()
    if not ENABLE_MEMORY or user_id is None:
        return False

    try:
        with _store_lock:
            store = _load_store_locked()
            filtered_items = []
            for item in store.get("items", []):
                owner = item.get("owner_user_id")
                if owner == user_id and item.get("scope") != "group_context":
                    continue
                filtered_items.append(item)
            store["items"] = filtered_items
            _save_store_locked(store)
        return True
    except Exception as e:
        logging.error("❌ WIPE MEMORY: Failed to wipe memory: %s", e, exc_info=True)
        return False


async def append_camera_log(camera_name: str, threat_report: str, scene_context: str) -> None:
    now_dt = datetime.now(USER_TIMEZONE)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M %Z")

    header = "# Emery Camera Security Log\n"
    new_entry = f"- [{now_str}] [{camera_name.strip()}] THREAT: {threat_report.strip()} | SCENE: {scene_context.strip()}\n"

    existing_lines = []
    if os.path.exists(CAMERA_LOG_FILE_PATH):
        try:
            with open(CAMERA_LOG_FILE_PATH, "r", encoding="utf-8") as f:
                existing_lines = f.readlines()
        except Exception as e:
            logging.error("❌ CAMERA LOG: Error reading camera log: %s", e, exc_info=True)

    cutoff_date = (now_dt - timedelta(days=7)).date()
    pruned_lines = []
    for line in existing_lines:
        if line.strip() == "# Emery Camera Security Log":
            continue
        match = re.match(r'^-\s+\[(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}(?:\s+\w+)?\]', line)
        if match:
            try:
                entry_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                if entry_date < cutoff_date:
                    continue
            except Exception:
                pass
        if line.strip():
            pruned_lines.append(line)

    out_lines = [header, "\n"] + [line for line in pruned_lines if line.strip()]
    if out_lines and not out_lines[-1].endswith("\n"):
        out_lines[-1] += "\n"
    out_lines.append(new_entry)

    try:
        with open(CAMERA_LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.writelines(out_lines)
        logging.debug("📹 CAMERA LOG: Logged activity for %s", camera_name)
    except Exception as e:
        logging.error("❌ CAMERA LOG: Failed to write camera log: %s", e, exc_info=True)


async def get_camera_security_log(camera_name: str = None, limit: int = 10) -> str:
    if not os.path.exists(CAMERA_LOG_FILE_PATH):
        return "No security camera logs are available."

    try:
        with open(CAMERA_LOG_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        entries = []
        for line in lines:
            if not line.strip() or line.startswith("#"):
                continue
            if camera_name:
                cleaned_filter = camera_name.strip().lower()
                match = re.search(r'^-\s+\[[^\]]+\]\s+\[([^\]]+)\]', line)
                if match:
                    entry_camera = match.group(1).strip().lower()
                    if cleaned_filter not in entry_camera and entry_camera not in cleaned_filter:
                        continue
                elif cleaned_filter not in line.lower():
                    continue
            entries.append(line.strip())

        if not entries:
            filter_msg = f" for camera '{camera_name}'" if camera_name else ""
            return f"No security camera logs found{filter_msg}."
        recent_entries = entries[-limit:]
        return "Recent Security Camera Logs:\n" + "\n".join(recent_entries)
    except Exception as e:
        logging.error("❌ CAMERA LOG: Error reading security log: %s", e, exc_info=True)
        return f"Failed to retrieve security logs: {e}"


def get_camera_log_summary() -> str:
    if not os.path.exists(CAMERA_LOG_FILE_PATH):
        return ""

    try:
        now_dt = datetime.now(USER_TIMEZONE)
        one_hour_ago = now_dt - timedelta(hours=1)
        with open(CAMERA_LOG_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        event_count = 0
        cameras_seen = set()
        for line in lines:
            if not line.strip() or line.startswith("#"):
                continue
            match = re.match(r'^-\s+\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:\s+\w+)?\]\s+\[([^\]]+)\]', line)
            if not match:
                continue
            try:
                entry_dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
                if one_hour_ago.replace(tzinfo=None) <= entry_dt <= now_dt.replace(tzinfo=None):
                    event_count += 1
                    cameras_seen.add(match.group(2).strip())
            except Exception:
                pass

        if event_count == 0:
            return ""
        camera_word = "camera" if len(cameras_seen) == 1 else "cameras"
        event_word = "event" if event_count == 1 else "events"
        return (
            f"{event_count} security camera {event_word} detected in the last hour across "
            f"{len(cameras_seen)} {camera_word} ({', '.join(sorted(cameras_seen))})"
        )
    except Exception as e:
        logging.error("❌ CAMERA LOG: Error summarizing logs: %s", e, exc_info=True)
        return ""


async def _store_topic_memory(topic_text: str, chat_id: int, user_id: int | None) -> None:
    if not topic_text:
        return
    metadata = {}
    if isinstance(topic_text, dict):
        metadata = dict(topic_text)
        topic_summary = (metadata.get("summary") or "").strip()
    else:
        topic_summary = str(topic_text).strip()
    if not topic_summary:
        return

    topic_class = (metadata.get("topic_class") or "").strip().lower()
    if topic_class not in {"private", "household", "group"}:
        if chat_id and chat_id < 0:
            topic_class = "group"
        elif _mentions_household(topic_summary):
            topic_class = "household"
        else:
            topic_class = "private"

    if topic_class == "group":
        scope, visibility = "group_context", "group_safe"
        owner_user_id = None
    elif topic_class == "household":
        scope, visibility = "shared_household", "household"
        owner_user_id = user_id
    else:
        scope, visibility = "private", "dm_only"
        owner_user_id = user_id

    tags = metadata.get("tags") or _extract_tags(topic_summary)
    metadata["tags"] = [tag for tag in tags if tag][:6]
    metadata["summary"] = topic_summary
    if "participants" in metadata and isinstance(metadata["participants"], list):
        metadata["participants"] = [str(name).strip() for name in metadata["participants"] if str(name).strip()][:6]
    if not metadata.get("channel_type"):
        metadata["channel_type"] = "Group Chat" if chat_id and chat_id < 0 else "DM"
    if not metadata.get("date_label"):
        metadata["date_label"] = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y")

    with _store_lock:
        store = _load_store_locked()
        _store_item_locked(
            store,
            owner_user_id=owner_user_id,
            source_user_id=user_id,
            source_chat_id=chat_id,
            item_type="topic",
            text=topic_summary,
            scope=scope,
            visibility=visibility,
            status="active",
            source_type="summary",
            tags=metadata["tags"],
            metadata=metadata,
        )
        _save_store_locked(store)
    asyncio.create_task(consolidate_memory_background())


async def summarize_topics_background(chat_id: int, user_id: int = None) -> None:
    global _last_summary_hist_len
    if user_id is None:
        user_id = globals.current_user_id.get()
    if chat_id in _topic_summary_chats:
        return

    history = list(globals.chat_histories.get(chat_id, []))
    hist_len = len(history)
    last_len = _last_summary_hist_len.get(chat_id, 0)
    if hist_len - last_len < 10:
        return

    _topic_summary_chats.add(chat_id)
    try:
        recent_history = history[max(last_len, hist_len - 12):]
        snippet = []
        participants = []
        seen_participants = set()
        for msg in recent_history:
            if msg.get("role") != "user":
                continue
            content = re.sub(r'<think>.*?</think>', '', msg.get("content", ""), flags=re.DOTALL | re.IGNORECASE).strip()
            if content.startswith("[System"):
                continue
            sender_name = msg.get("sender_name") or "User"
            sender_user_id = msg.get("user_id")
            sender_key = f"{sender_name.lower()}::{sender_user_id}"
            if sender_key not in seen_participants:
                seen_participants.add(sender_key)
                participants.append({
                    "name": sender_name,
                    "user_id": sender_user_id,
                })
            snippet.append(f"{sender_name.upper()}: {content}")
        _last_summary_hist_len[chat_id] = hist_len
        if len(snippet) < 2:
            return

        now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y")
        chat_type = "DM" if chat_id > 0 else "Group Chat"
        allowed_names = [p["name"].upper() for p in participants if p.get("name")]
        system_prompt = (
            "You are a strict JSON generator for durable topic memory.\n"
            "Return ONLY valid JSON.\n"
            "No markdown, no prose, no comments, no explanation.\n"
            "The JSON must parse exactly as returned.\n"
            "Return JSON with exactly this shape:\n"
            "{\"topics\":[{\"summary\":\"...\",\"topic_class\":\"private|household|group\",\"tags\":[\"...\"],\"participants\":[\"...\"],\"confidence\":0.0}]}\n"
            "Decision rules:\n"
            "1. Include a topic only if it is genuinely worth long-term memory.\n"
            "2. Prefer fewer topics; if one topic can cover the conversation cleanly, return one topic.\n"
            "3. If two candidate topics overlap materially, keep only the stronger one.\n"
            "4. Every returned topic MUST contain all 5 required keys: summary, topic_class, tags, participants, confidence.\n"
            "5. tags must contain 2 to 5 items only.\n"
            "6. participants must be copied exactly from the allowed participant list and may not include any other values.\n"
            "7. confidence must always be a number from 0.0 to 1.0.\n"
            "8. topic_class must be exactly one of private, household, or group.\n"
            "9. If nothing is worth remembering, return exactly {\"topics\":[]}."
        )
        known_names = sorted(_primary_known_names())
        participant_names = ", ".join(p["name"] for p in participants if p.get("name"))
        user_prompt = (
            f"Date: {now_str}\n"
            f"Channel: {chat_type}\n"
            f"Known household names: {', '.join(known_names)}\n"
            f"Observed participants: {participant_names}\n\n"
            f"Allowed participant list: {allowed_names}\n\n"
            "Conversation snippet:\n\n" + "\n".join(snippet)
        )

        from emery.helpers import query_fast_model
        summary = (await query_fast_model(user_prompt, system_prompt)).strip()
        if not summary or summary.upper() == "NONE":
            return
        try:
            payload = json.loads(summary)
        except Exception:
            logging.warning("⚠️ TOPIC MONITOR: Invalid topic JSON, skipping payload: %s", safe_preview(summary, max_len=200))
            return

        _debug_topic_payloads("raw_model_payload", payload)

        topics = payload.get("topics") if isinstance(payload, dict) else None
        default_topic_class = "group" if chat_id < 0 and len(participants) > 1 else "private"
        normalized_topics = _normalize_topic_payloads(
            topics,
            allowed_names=allowed_names,
            default_topic_class=default_topic_class,
        )
        _debug_topic_payloads("normalized_topics", normalized_topics)
        if not normalized_topics:
            logging.info("🧭 TOPIC DEBUG [normalized_topics]: no valid topics survived normalization")
            return

        participant_name_map = {p["name"].lower(): p["user_id"] for p in participants if p.get("name")}
        for topic in normalized_topics:
            cleaned_summary = topic["summary"]
            topic_class = topic["topic_class"]
            raw_participants = topic["participants"]
            cleaned_participants = []
            participant_user_ids = []
            for name in raw_participants:
                clean_name = str(name).strip()
                if not clean_name:
                    continue
                cleaned_participants.append(clean_name)
                user_match = participant_name_map.get(clean_name.lower())
                if user_match:
                    participant_user_ids.append(user_match)
            topic_payload = {
                "summary": cleaned_summary,
                "topic_class": topic_class,
                "tags": topic["tags"],
                "participants": cleaned_participants,
                "participant_user_ids": participant_user_ids[:6],
                "channel_type": chat_type,
                "date_label": now_str,
                "confidence": topic["confidence"],
            }
            topic_owner_id = user_id
            if topic_class == "private" and len(set(participant_user_ids)) == 1:
                topic_owner_id = participant_user_ids[0]
            _debug_topic_payloads("final_topic_payload", {
                "topic_owner_id": topic_owner_id,
                "chat_id": chat_id,
                "payload": topic_payload,
            })
            await _store_topic_memory(topic_payload, chat_id, topic_owner_id)
    except Exception as e:
        logging.error("❌ TOPIC MONITOR: Background topic summary crash: %s", e, exc_info=True)
    finally:
        _topic_summary_chats.discard(chat_id)
