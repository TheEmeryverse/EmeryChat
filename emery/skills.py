"""Durable procedural skills for Emery.

Skills are reusable, human-readable playbooks.  They are deliberately kept
separate from personal memory: memory stores facts, while a skill stores a
repeatable way of accomplishing a task.  A skill never grants a capability;
the normal tool registry, privacy rules, and command approval layer remain the
authority for execution.
"""

from __future__ import annotations

import copy
import ast
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import emery.globals as globals
from emery.config import SKILL_MAX_CHARS, SKILL_MAX_RETRIEVAL_CHARS, SKILLS_STORE_PATH, SKILL_WRITE_APPROVAL


STORE_VERSION = 1
MAX_SKILLS_PER_SCOPE = 200
MAX_RETRIEVED_SKILLS = 3
MAX_TRIGGERS = 20
MAX_REFERENCED_TOOLS = 20
MAX_VERSION_HISTORY = 20
SKILL_DOCUMENT_NAME = "SKILL.md"
SKILL_SUPPORT_DIRECTORIES = ("references", "templates", "scripts", "assets")
MAX_SUPPORTING_FILES = 200
MAX_SUPPORTING_FILE_BYTES = 2 * 1024 * 1024
FILESYSTEM_INDEX_VERSION = 1
# The JSON store remains the compatibility cache.  By default, Markdown skill
# directories live next to it (data/skills/<category>/<slug>/SKILL.md).  An
# explicit root is useful for deployments that mount skills separately.
SKILLS_FILESYSTEM_PATH = os.getenv("SKILLS_FILESYSTEM_PATH")
_store_lock = threading.RLock()


class SkillError(ValueError, PermissionError):
    """Expected skill validation or scope error."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_store() -> dict:
    return {"version": STORE_VERSION, "next_id": 1, "skills": []}


def _store_path() -> Path:
    return Path(SKILLS_STORE_PATH).expanduser()


def skill_filesystem_root() -> Path:
    """Return the root containing category/<slug>/SKILL.md directories."""
    configured = SKILLS_FILESYSTEM_PATH or os.getenv("SKILLS_FILESYSTEM_PATH")
    return Path(configured).expanduser() if configured else _store_path().parent


def _atomic_write_text(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    if isinstance(content, bytes):
        temp.write_bytes(content)
    else:
        temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def _safe_component(value: Any, *, field: str, default: str | None = None) -> str:
    text = str(value if value is not None else (default or "")).strip()
    if not text:
        if default is not None:
            text = default
        else:
            raise SkillError(f"{field} is required")
    if "\x00" in text or text in {".", ".."} or "/" in text or "\\" in text:
        raise SkillError(f"invalid {field}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", text):
        raise SkillError(f"invalid {field}")
    return text


def _safe_relative_path(relative_path: Any, *, allow_skill_document: bool = False) -> str:
    """Validate a supporting-file path and return its normalized POSIX form."""
    text = str(relative_path or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise SkillError("file path must be relative")
    parts = text.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise SkillError("file path contains an unsafe component")
    if parts == [SKILL_DOCUMENT_NAME]:
        if not allow_skill_document:
            raise SkillError("SKILL.md is managed by the skill document API")
        return SKILL_DOCUMENT_NAME
    if parts[0] not in SKILL_SUPPORT_DIRECTORIES:
        raise SkillError(f"supporting files must be under {', '.join(SKILL_SUPPORT_DIRECTORIES)}")
    for part in parts:
        _safe_component(part, field="file path component")
    return "/".join(parts)


def _safe_filesystem_relative_path(relative_path: Any) -> str:
    text = str(relative_path or "").strip().replace("\\", "/")
    parts = text.split("/")
    if len(parts) != 3 or parts[2] != SKILL_DOCUMENT_NAME:
        raise SkillError("invalid filesystem skill path")
    if any(not part or part in {".", ".."} for part in parts):
        raise SkillError("filesystem skill path contains an unsafe component")
    _safe_component(parts[0], field="category")
    _safe_component(parts[1], field="slug")
    return "/".join(parts)


def _safe_skill_directory(category: str, slug: str, *, create: bool = False) -> Path:
    root = skill_filesystem_root()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    category = _safe_component(category, field="category", default="general")
    slug = _safe_component(slug, field="slug")
    candidate = (root / category / slug).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SkillError("skill path escapes the filesystem root") from exc
    if create:
        # Do not follow a pre-existing symlink into another skill or directory.
        for parent in (root / category, candidate):
            if parent.is_symlink():
                raise SkillError("skill path may not contain symlinks")
        candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _safe_skill_file(skill_dir: Path, relative_path: str, *, create: bool = False) -> Path:
    normalized = _safe_relative_path(relative_path, allow_skill_document=True)
    root = skill_dir.resolve()
    candidate = (skill_dir / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SkillError("file path escapes the skill directory") from exc
    if create:
        current = skill_dir
        for part in Path(normalized).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise SkillError("supporting file path may not contain symlinks")
        if candidate.exists() and candidate.is_symlink():
            raise SkillError("supporting file path may not contain symlinks")
        candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _yaml_value(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value in {"|", ">"}:
        return ""
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    if value.casefold() in {"null", "~"}:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value.strip("'\"")


def _parse_frontmatter(document: str) -> tuple[dict[str, Any], str]:
    """Parse the small, intentionally conservative YAML subset we emit."""
    if not document.startswith("---"):
        return {}, document
    lines = document.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, document
    end = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
    if end is None:
        raise SkillError("SKILL.md frontmatter is not closed")
    metadata: dict[str, Any] = {}
    index = 1
    while index < end:
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        match = re.match(r"^\s*([A-Za-z0-9_-]+):(?:\s*(.*))?$", line)
        if not match:
            raise SkillError("invalid SKILL.md frontmatter")
        key, raw = match.group(1), match.group(2) or ""
        if raw in {"|", ">"}:
            collected = []
            index += 1
            while index < end and (not lines[index].strip() or lines[index].startswith((" ", "\t"))):
                collected.append(lines[index].lstrip())
                index += 1
            metadata[key] = ("\n" if raw == "|" else " ").join(collected).strip()
            continue
        if raw:
            metadata[key] = _yaml_value(raw)
            index += 1
            continue
        items = []
        next_index = index + 1
        while next_index < end and re.match(r"^\s*-\s+", lines[next_index]):
            items.append(_yaml_value(re.sub(r"^\s*-\s+", "", lines[next_index])))
            next_index += 1
        metadata[key] = items if items else ""
        index = next_index
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return metadata, body


def _frontmatter_value(value: Any) -> str:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _render_skill_document(skill: Mapping[str, Any]) -> str:
    fields = (
        "id", "name", "description", "category", "scope", "owner_user_id", "chat_id",
        "status", "version", "triggers", "prerequisites", "tools", "verification", "failure_modes",
    )
    frontmatter = ["---"]
    for field in fields:
        if field in skill and skill.get(field) is not None:
            frontmatter.append(f"{field}: {_frontmatter_value(skill.get(field))}")
    frontmatter.extend(["---", ""])
    return "\n".join(frontmatter) + str(skill.get("procedure") or "").rstrip() + "\n"


def _supporting_files_for_directory(skill_dir: Path) -> list[dict[str, Any]]:
    if not skill_dir.exists():
        return []
    result = []
    for directory in SKILL_SUPPORT_DIRECTORIES:
        base = skill_dir / directory
        if not base.is_dir() or base.is_symlink():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file() and not item.is_symlink()):
            relative = path.relative_to(skill_dir).as_posix()
            try:
                _safe_relative_path(relative)
            except SkillError:
                continue
            result.append({"path": relative, "size": path.stat().st_size})
            if len(result) >= MAX_SUPPORTING_FILES:
                return result
    return result


def _read_skill_document(path: Path) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    try:
        document = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError("SKILL.md must be UTF-8 text") from exc
    if len(document) > SKILL_MAX_CHARS + 20_000:
        raise SkillError("SKILL.md is too large")
    metadata, procedure = _parse_frontmatter(document)
    category = path.parent.parent.name
    metadata.setdefault("category", category)
    metadata.setdefault("slug", path.parent.name)
    metadata.setdefault("name", path.parent.name)
    metadata.setdefault("description", "")
    metadata["procedure"] = procedure.strip()
    metadata["filesystem_path"] = path.relative_to(skill_filesystem_root().resolve()).as_posix()
    metadata["files"] = _supporting_files_for_directory(path.parent)
    return metadata, procedure.strip(), metadata["files"]


def _discover_filesystem_records() -> list[dict[str, Any]]:
    root = skill_filesystem_root()
    if not root.exists() or not root.is_dir():
        return []
    records = []
    for path in sorted(root.rglob(SKILL_DOCUMENT_NAME)):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            record, _procedure, _files = _read_skill_document(path)
            record["_filesystem_path"] = record.get("filesystem_path")
            records.append(record)
        except (OSError, SkillError, ValueError) as exc:
            logging.warning("SKILLS: ignoring invalid filesystem skill %s (%s)", path, exc)
    return records


def _filesystem_path_for_skill(skill: Mapping[str, Any], *, create: bool = False) -> Path:
    stored = skill.get("filesystem_path") or skill.get("_filesystem_path")
    if stored:
        normalized = _safe_filesystem_relative_path(stored)
        root = skill_filesystem_root().resolve()
        path = (root / normalized).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SkillError("filesystem skill path escapes root") from exc
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return _safe_skill_directory(
        str(skill.get("category") or "general"),
        str(skill.get("slug") or _slugify(str(skill.get("name") or "skill"))),
        create=create,
    ) / SKILL_DOCUMENT_NAME


def _hydrate_from_filesystem(skill: dict[str, Any]) -> dict[str, Any]:
    try:
        path = _filesystem_path_for_skill(skill)
        if not path.is_file():
            return skill
        metadata, procedure, files = _read_skill_document(path)
    except (OSError, SkillError):
        return skill
    for field in (
        "name", "slug", "description", "category", "scope", "owner_user_id", "chat_id",
        "status", "version", "triggers", "prerequisites", "tools", "verification", "failure_modes",
    ):
        if field in metadata:
            skill[field] = metadata[field]
    skill["procedure"] = procedure
    skill["filesystem_path"] = metadata.get("filesystem_path")
    skill["files"] = files
    return skill


def _merge_filesystem_records(store: dict[str, Any]) -> None:
    """Import hand-authored SKILL.md files into the JSON compatibility cache."""
    for record in _discover_filesystem_records():
        record_id = str(record.get("id") or "")
        existing = next(
            (item for item in store["skills"] if isinstance(item, dict) and (
                (record_id and str(item.get("id")) == record_id)
                or (
                    item.get("slug") == record.get("slug")
                    and item.get("category", "general") == record.get("category", "general")
                    and item.get("scope") == record.get("scope")
                    and item.get("chat_id") == record.get("chat_id")
                    and item.get("owner_user_id") == record.get("owner_user_id")
                )
            )),
            None,
        )
        if existing is None:
            safe_id = record_id or f"skill_fs_{_slugify(str(record.get('category', 'general')) + '-' + str(record.get('slug', 'skill')))}"
            used_ids = {str(item.get("id")) for item in store["skills"] if isinstance(item, dict)}
            if safe_id in used_ids:
                safe_id = f"{safe_id}_{len(used_ids) + 1}"
            existing = {
                "id": safe_id,
                "version": int(record.get("version", 1) or 1),
                "created_at": _utc_now_iso(),
                "use_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "versions": [],
            }
            store["skills"].append(existing)
        for field, value in record.items():
            if not field.startswith("_") and field not in {"filesystem_path"}:
                existing[field] = copy.deepcopy(value)
        existing["filesystem_path"] = record.get("filesystem_path")
        existing["_filesystem_path"] = record.get("filesystem_path")
        existing.setdefault("updated_at", _utc_now_iso())


def _write_filesystem_index() -> None:
    root = skill_filesystem_root()
    root.mkdir(parents=True, exist_ok=True)
    compact = []
    for record in _discover_filesystem_records():
        compact.append({
            key: copy.deepcopy(record.get(key))
            for key in (
                "id", "name", "slug", "description", "category", "scope", "owner_user_id", "chat_id",
                "status", "version", "triggers", "tools", "filesystem_path", "files",
            ) if key in record
        })
    _atomic_write(root / "skills.index.json", {
        "version": FILESYSTEM_INDEX_VERSION,
        "generated_at": _utc_now_iso(),
        "skills": compact,
    })


def _normalize_supporting_files(
    *,
    files: Mapping[str, Any] | None = None,
    references: Mapping[str, Any] | None = None,
    templates: Mapping[str, Any] | None = None,
    scripts: Mapping[str, Any] | None = None,
    assets: Mapping[str, Any] | None = None,
) -> dict[str, str | bytes]:
    """Normalize optional supporting-file groups without allowing path escape."""
    normalized: dict[str, str | bytes] = {}

    def add_group(directory: str, values: Mapping[str, Any] | None) -> None:
        if values is None:
            return
        if not isinstance(values, Mapping):
            raise SkillError(f"{directory} must be a mapping of relative paths to contents")
        for name, content in values.items():
            relative = str(name).replace("\\", "/")
            if not relative.startswith(directory + "/"):
                relative = f"{directory}/{relative}"
            _safe_relative_path(relative)
            if not isinstance(content, (str, bytes)):
                raise SkillError(f"{directory} contents must be text or bytes")
            if len(content) > MAX_SUPPORTING_FILE_BYTES:
                raise SkillError(f"supporting files are limited to {MAX_SUPPORTING_FILE_BYTES} bytes")
            normalized[relative] = content

    if files is not None:
        if not isinstance(files, Mapping):
            raise SkillError("files must be a mapping of relative paths to contents")
        for name, content in files.items():
            relative = _safe_relative_path(name)
            if not isinstance(content, (str, bytes)):
                raise SkillError("file contents must be text or bytes")
            if len(content) > MAX_SUPPORTING_FILE_BYTES:
                raise SkillError(f"supporting files are limited to {MAX_SUPPORTING_FILE_BYTES} bytes")
            normalized[relative] = content
    add_group("references", references)
    add_group("templates", templates)
    add_group("scripts", scripts)
    add_group("assets", assets)
    if len(normalized) > MAX_SUPPORTING_FILES:
        raise SkillError(f"a skill may contain at most {MAX_SUPPORTING_FILES} supporting files")
    return normalized


def _write_skill_filesystem(skill: dict[str, Any], *, supporting_files: Mapping[str, str | bytes] | None = None) -> Path:
    document_path = _filesystem_path_for_skill(skill, create=True)
    skill["filesystem_path"] = document_path.relative_to(skill_filesystem_root().resolve()).as_posix()
    _atomic_write_text(document_path, _render_skill_document(skill))
    if supporting_files:
        for relative, content in supporting_files.items():
            path = _safe_skill_file(document_path.parent, relative, create=True)
            _atomic_write_text(path, content)
    skill["files"] = _supporting_files_for_directory(document_path.parent)
    _write_filesystem_index()
    return document_path


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _load_store_locked() -> dict:
    path = _store_path()
    if not path.exists():
        store = _default_store()
        _merge_filesystem_records(store)
        _atomic_write(path, store)
        return store
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
            raise ValueError("invalid skill store structure")
        data.setdefault("version", STORE_VERSION)
        data.setdefault("next_id", len(data["skills"]) + 1)
        _merge_filesystem_records(data)
        numeric_ids = [
            int(match.group(1))
            for item in data["skills"]
            if isinstance(item, dict)
            for match in [re.fullmatch(r"skill_(\d+)", str(item.get("id", "")))]
            if match
        ]
        data["next_id"] = max([int(data.get("next_id", 1) or 1), *(number + 1 for number in numeric_ids)])
        return data
    except Exception as exc:
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            if path.exists():
                os.replace(path, backup)
            repaired = _default_store()
            _atomic_write(path, repaired)
            logging.error("SKILLS: repaired corrupt store %s (%s)", path, exc)
            return repaired
        except Exception:
            logging.error("SKILLS: unable to repair store %s", path, exc_info=True)
            return _default_store()


def _save_store_locked(store: dict) -> None:
    store["version"] = STORE_VERSION
    _atomic_write(_store_path(), store)


def _clean_text(value: Any, *, limit: int, field: str) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise SkillError(f"{field} is limited to {limit} characters")
    return text


def _clean_list(value: Any, *, limit: int, item_limit: int, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise SkillError(f"{field} must be a list of strings")
    if len(value) > limit:
        raise SkillError(f"{field} is limited to {limit} items")
    result = []
    seen = set()
    for item in value:
        text = _clean_text(item, limit=item_limit, field=field)
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:80] or "skill"


def _scope_context(*, chat_id: int | None = None, user_id: int | None = None, use_context: bool = True) -> tuple[int | None, int | None, str, int | None]:
    if use_context:
        if chat_id is None:
            chat_id = globals.TARGET_CHAT_ID.get()
        if user_id is None:
            user_id = globals.current_user_id.get()
    try:
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        chat_id = None
    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    if chat_id is not None and chat_id < 0:
        return chat_id, user_id, "group", None
    return chat_id, user_id, "private", user_id


def _normalize_scope(scope: str | None, *, chat_id: int | None, owner_user_id: int | None) -> tuple[str, int | None, int | None]:
    requested = str(scope or "").strip().casefold()
    if not requested:
        requested = "group" if chat_id is not None and chat_id < 0 else "private"
    if requested not in {"private", "group"}:
        raise SkillError("scope must be private or group; global skills are not enabled")
    if requested == "private":
        if owner_user_id is None:
            raise SkillError("a private skill requires an identified user")
        return requested, None, owner_user_id
    if chat_id is None or chat_id >= 0:
        raise SkillError("a group skill requires an active group chat")
    return requested, chat_id, None


def _visible(skill: Mapping[str, Any], *, chat_id: int | None, user_id: int | None, include_drafts: bool = False, include_archived: bool = False) -> bool:
    allowed_statuses = {"active"}
    if include_drafts:
        allowed_statuses.add("draft")
    if include_archived:
        allowed_statuses.add("archived")
    if skill.get("status") not in allowed_statuses:
        return False
    scope = skill.get("scope")
    if scope == "private":
        return user_id is not None and skill.get("owner_user_id") == user_id and not (chat_id is not None and chat_id < 0)
    if scope == "group":
        return chat_id is not None and chat_id < 0 and skill.get("chat_id") == chat_id
    return False


def _public_skill(skill: Mapping[str, Any], *, include_content: bool = True) -> dict:
    hydrated = _hydrate_from_filesystem(dict(skill))
    fields = (
        "id", "name", "slug", "description", "scope", "status", "version",
        "triggers", "prerequisites", "tools", "created_at", "updated_at",
        "last_used_at", "use_count", "success_count", "failure_count",
        "category", "filesystem_path", "files",
    )
    result = {field: copy.deepcopy(hydrated.get(field)) for field in fields if field in hydrated}
    if include_content:
        result["procedure"] = hydrated.get("procedure", "")
        result["instructions"] = hydrated.get("procedure", "")
        result["verification"] = hydrated.get("verification", "")
        result["failure_modes"] = hydrated.get("failure_modes", "")
    return result


def _find_skill_locked(store: Mapping[str, Any], identifier: str, *, chat_id: int | None, user_id: int | None, include_drafts: bool = False, include_archived: bool = False) -> dict | None:
    needle = str(identifier or "").strip().casefold()
    for skill in store.get("skills", []):
        if not isinstance(skill, dict) or not _visible(skill, chat_id=chat_id, user_id=user_id, include_drafts=include_drafts, include_archived=include_archived):
            continue
        if needle in {str(skill.get("id", "")).casefold(), str(skill.get("slug", "")).casefold(), str(skill.get("name", "")).casefold()}:
            return skill
    return None


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{2,}", str(text or "").casefold())}


def _score_skill(skill: Mapping[str, Any], query: str) -> float:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0.0
    title_tokens = _tokenize(f"{skill.get('name', '')} {skill.get('description', '')}")
    trigger_tokens = _tokenize(" ".join(skill.get("triggers") or []))
    body_tokens = _tokenize(
        f"{skill.get('procedure', '')} {skill.get('prerequisites', '')} {skill.get('tools', '')}"
    )
    score = len(query_tokens & title_tokens) * 5.0
    score += len(query_tokens & trigger_tokens) * 4.0
    score += len(query_tokens & body_tokens) * 1.0
    if query.casefold().strip() in {str(t).casefold() for t in skill.get("triggers") or []}:
        score += 10.0
    return score / max(1.0, len(query_tokens) ** 0.5)


def search_skills(query: str, *, limit: int = MAX_RETRIEVED_SKILLS, include_drafts: bool = False, chat_id: int | None = None, user_id: int | None = None) -> list[dict]:
    """Return visible skills ranked by lexical relevance."""
    if chat_id is None and user_id is None:
        chat_id, user_id, _scope, _owner = _scope_context()
    try:
        limit = max(1, min(int(limit), MAX_RETRIEVED_SKILLS * 3))
    except (TypeError, ValueError):
        limit = MAX_RETRIEVED_SKILLS
    with _store_lock:
        store = _load_store_locked()
        matches = [
            (score, skill)
            for skill in store.get("skills", [])
            if isinstance(skill, dict)
            and _visible(skill, chat_id=chat_id, user_id=user_id, include_drafts=include_drafts)
            and (score := _score_skill(skill, query)) > 0
        ]
        matches.sort(key=lambda pair: (-pair[0], -int(pair[1].get("use_count", 0) or 0), pair[1].get("name", "")))
        return [_public_skill(skill, include_content=False) | {"relevance": round(score, 3)} for score, skill in matches[:limit]]


def retrieve_relevant_skills(
    user_query: str,
    user_id: int | None = None,
    chat_id: int | None = None,
    *,
    limit: int = MAX_RETRIEVED_SKILLS,
    include_content: bool = True,
) -> list[dict]:
    """Return relevant skills.

    ``include_content=True`` preserves Emery's historical prompt API.  New
    callers should leave it false and use :func:`view_skill` only after a
    compact result has been selected; this is the progressive-disclosure path.
    """
    if chat_id is None and user_id is None:
        chat_id = globals.TARGET_CHAT_ID.get()
    if user_id is None and chat_id is None:
        user_id = globals.current_user_id.get()
    matches = search_skills(user_query, limit=limit, chat_id=chat_id, user_id=user_id)
    if not matches:
        return []
    selected = []
    with _store_lock:
        store = _load_store_locked()
        for summary in matches:
            skill = next((item for item in store.get("skills", []) if item.get("id") == summary.get("id")), None)
            if not skill:
                continue
            selected.append(_public_skill(skill, include_content=include_content) | {"relevance": summary.get("relevance")})
    return selected


def format_relevant_skills(skills: list[Mapping[str, Any]] | None) -> str:
    """Render retrieved skills into bounded, cache-safe turn context."""
    if not skills:
        return ""
    blocks = []
    used = 0
    for skill in skills:
        procedure = skill.get("procedure") or skill.get("instructions") or ""
        if not procedure:
            block = (
                f"## {skill.get('name')} ({skill.get('id')})\n"
                f"Purpose: {skill.get('description', '')}\n"
                f"Category: {skill.get('category') or 'general'}\n"
                f"Triggers: {', '.join(skill.get('triggers') or []) or 'none listed'}\n"
                f"Referenced tools: {', '.join(skill.get('tools') or []) or 'use the best available tools'}\n"
                "Full procedure: call `skill_view` before applying this skill."
            )
        else:
            block = (
                f"## {skill.get('name')} ({skill.get('id')})\n"
                f"Purpose: {skill.get('description', '')}\n"
                f"Triggers: {', '.join(skill.get('triggers') or []) or 'none listed'}\n"
                f"Prerequisites: {skill.get('prerequisites') or 'none listed'}\n"
                f"Referenced tools: {', '.join(skill.get('tools') or []) or 'use the best available tools'}\n"
                f"Procedure:\n{procedure}\n"
                f"Verification:\n{skill.get('verification') or 'Verify tool results before claiming success.'}\n"
                f"Failure modes:\n{skill.get('failure_modes') or 'If a step fails, report it and adapt safely.'}"
            )
        if blocks and used + len(block) + 2 > SKILL_MAX_RETRIEVAL_CHARS:
            break
        blocks.append(block)
        used += len(block) + 2
    if not blocks:
        return ""
    return (
        "# Relevant Durable Skills\n"
        "These are reusable playbooks, not permissions. Follow them only when they fit the request. "
        "The normal tool schemas, privacy rules, approval requirements, and user instructions always win.\n\n"
        + "\n\n".join(blocks)
    )


def save_skill(
    name: str,
    description: str,
    procedure: str,
    *,
    triggers: Any = None,
    prerequisites: str = "",
    verification: str = "",
    failure_modes: str = "",
    tools: Any = None,
    scope: str | None = None,
    status: str = "draft",
    user_id: int | None = None,
    chat_id: int | None = None,
    category: str | None = None,
    files: Mapping[str, Any] | None = None,
    references: Mapping[str, Any] | None = None,
    templates: Mapping[str, Any] | None = None,
    scripts: Mapping[str, Any] | None = None,
    assets: Mapping[str, Any] | None = None,
) -> dict:
    """Create or replace a visible skill in the active chat scope."""
    name = _clean_text(name, limit=120, field="name")
    description = _clean_text(description, limit=600, field="description")
    procedure = _clean_text(procedure, limit=SKILL_MAX_CHARS, field="procedure")
    if not name or not description or not procedure:
        raise SkillError("name, description, and procedure are required")
    status = str(status or "draft").strip().casefold()
    if status not in {"draft", "active"}:
        raise SkillError("status must be draft or active")
    chat_id, user_id, _current_scope, owner = _scope_context(
        chat_id=chat_id,
        user_id=user_id,
        use_context=user_id is None and chat_id is None,
    )
    scope, skill_chat_id, skill_owner = _normalize_scope(scope, chat_id=chat_id, owner_user_id=owner)
    if scope == "group":
        skill_owner = None
    clean_triggers = _clean_list(triggers, limit=MAX_TRIGGERS, item_limit=160, field="triggers")
    clean_tools = _clean_list(tools, limit=MAX_REFERENCED_TOOLS, item_limit=120, field="tools")
    prerequisites = _clean_text(prerequisites, limit=1200, field="prerequisites")
    verification = _clean_text(verification, limit=2000, field="verification")
    failure_modes = _clean_text(failure_modes, limit=2000, field="failure_modes")
    slug = _slugify(name)
    requested_category = category
    supporting_files = _normalize_supporting_files(
        files=files, references=references, templates=templates, scripts=scripts, assets=assets,
    )
    now = _utc_now_iso()
    with _store_lock:
        store = _load_store_locked()
        existing = next(
            (
                skill for skill in store["skills"]
                if skill.get("slug") == slug
                and skill.get("scope") == scope
                and skill.get("chat_id") == skill_chat_id
                and skill.get("owner_user_id") == skill_owner
            ),
            None,
        )
        if existing is None:
            scope_count = sum(1 for skill in store["skills"] if skill.get("scope") == scope and skill.get("chat_id") == skill_chat_id and skill.get("owner_user_id") == skill_owner)
            if scope_count >= MAX_SKILLS_PER_SCOPE:
                raise SkillError(f"skill limit reached for {scope} scope")
            existing = {
                "id": f"skill_{store['next_id']}",
                "version": 1,
                "created_at": now,
                "use_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "versions": [],
                "scope": scope,
                "chat_id": skill_chat_id,
                "owner_user_id": skill_owner,
            }
            store["next_id"] += 1
            store["skills"].append(existing)
        else:
            existing.setdefault("versions", []).append({
                "version": existing.get("version", 1),
                "updated_at": existing.get("updated_at", existing.get("created_at", now)),
                "procedure": existing.get("procedure", ""),
                "description": existing.get("description", ""),
            })
            existing["versions"] = existing["versions"][-MAX_VERSION_HISTORY:]
            existing["version"] = int(existing.get("version", 1) or 1) + 1
        existing.update({
            "name": name,
            "slug": slug,
            "description": description,
            "procedure": procedure,
            "triggers": clean_triggers,
            "prerequisites": prerequisites,
            "verification": verification,
            "failure_modes": failure_modes,
            "tools": clean_tools,
            "category": _safe_component(
                requested_category if requested_category is not None else existing.get("category", "general"),
                field="category",
            ),
            "status": status,
            "updated_at": now,
        })
        # The Markdown representation is written before the JSON cache.  A
        # failed filesystem write therefore cannot leave the cache claiming a
        # skill was saved when its human-readable source was not persisted.
        _write_skill_filesystem(existing, supporting_files=supporting_files)
        _save_store_locked(store)
        return _public_skill(existing)


def read_skill(identifier: str, *, include_drafts: bool = True, include_archived: bool = True, user_id: int | None = None, chat_id: int | None = None) -> dict:
    chat_id, user_id, _scope, _owner = _scope_context(
        chat_id=chat_id, user_id=user_id, use_context=user_id is None and chat_id is None,
    )
    with _store_lock:
        skill = _find_skill_locked(
            _load_store_locked(), identifier, chat_id=chat_id, user_id=user_id,
            include_drafts=include_drafts, include_archived=include_archived,
        )
        if skill is None:
            raise SkillError("skill not found in the current scope")
        return _public_skill(skill)


def view_skill(
    identifier: str,
    *,
    include_files: bool = True,
    include_drafts: bool = True,
    include_archived: bool = True,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> dict:
    """Load the complete SKILL.md view on demand, including file metadata."""
    result = read_skill(
        identifier,
        include_drafts=include_drafts,
        include_archived=include_archived,
        user_id=user_id,
        chat_id=chat_id,
    )
    if include_files:
        result["files"] = list(result.get("files") or [])
    return result


def _visible_skill_for_file_operation(
    identifier: str, *, user_id: int | None, chat_id: int | None,
) -> dict:
    chat_id, user_id, _scope, _owner = _scope_context(
        chat_id=chat_id, user_id=user_id, use_context=user_id is None and chat_id is None,
    )
    with _store_lock:
        skill = _find_skill_locked(
            _load_store_locked(), identifier, chat_id=chat_id, user_id=user_id,
            include_drafts=True, include_archived=True,
        )
        if skill is None:
            raise SkillError("skill not found in the current scope")
        return copy.deepcopy(skill)


def list_skill_files(identifier: str, *, user_id: int | None = None, chat_id: int | None = None) -> list[dict[str, Any]]:
    """List safe supporting files without loading their contents."""
    skill = _visible_skill_for_file_operation(identifier, user_id=user_id, chat_id=chat_id)
    path = _filesystem_path_for_skill(skill)
    return _supporting_files_for_directory(path.parent)


def read_skill_file(relative_path: str, *, identifier: str, user_id: int | None = None, chat_id: int | None = None) -> str | bytes:
    """Read one references/templates/scripts/assets file after scope checks."""
    skill = _visible_skill_for_file_operation(identifier, user_id=user_id, chat_id=chat_id)
    path = _safe_skill_file(_filesystem_path_for_skill(skill).parent, relative_path)
    if not path.is_file():
        raise SkillError("supporting file not found")
    if path.stat().st_size > MAX_SUPPORTING_FILE_BYTES:
        raise SkillError("supporting file is too large")
    content = path.read_bytes()
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content


def write_skill_file(
    relative_path: str,
    content: str | bytes,
    *,
    identifier: str,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> dict:
    """Write one safe supporting file and refresh the compact filesystem index."""
    if not isinstance(content, (str, bytes)) or len(content) > MAX_SUPPORTING_FILE_BYTES:
        raise SkillError(f"supporting file contents are limited to {MAX_SUPPORTING_FILE_BYTES} bytes")
    skill = _visible_skill_for_file_operation(identifier, user_id=user_id, chat_id=chat_id)
    path = _safe_skill_file(_filesystem_path_for_skill(skill, create=True).parent, relative_path, create=True)
    _atomic_write_text(path, content)
    _write_filesystem_index()
    return {"path": _safe_relative_path(relative_path), "size": path.stat().st_size}


def remove_skill_file(
    relative_path: str,
    *,
    identifier: str,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> dict:
    """Remove one safe supporting file after scope checks."""
    skill = _visible_skill_for_file_operation(identifier, user_id=user_id, chat_id=chat_id)
    path = _safe_skill_file(_filesystem_path_for_skill(skill).parent, relative_path)
    if not path.is_file() or path.is_symlink():
        raise SkillError("supporting file not found")
    path.unlink()
    _write_filesystem_index()
    return {"path": _safe_relative_path(relative_path), "removed": True}


def migrate_json_skills_to_filesystem() -> int:
    """Materialize legacy JSON-only skills as SKILL.md directories."""
    written = 0
    with _store_lock:
        store = _load_store_locked()
        for skill in store.get("skills", []):
            if not isinstance(skill, dict):
                continue
            _write_skill_filesystem(skill)
            written += 1
        _save_store_locked(store)
    return written


def list_skills(*, include_drafts: bool = True, user_id: int | None = None, chat_id: int | None = None) -> list[dict]:
    chat_id, user_id, _scope, _owner = _scope_context(
        chat_id=chat_id, user_id=user_id, use_context=user_id is None and chat_id is None,
    )
    with _store_lock:
        store = _load_store_locked()
        return [
            _public_skill(skill, include_content=False)
            for skill in store.get("skills", [])
            if isinstance(skill, dict) and _visible(skill, chat_id=chat_id, user_id=user_id, include_drafts=include_drafts)
        ]


def set_skill_status(identifier: str, status: str, *, user_id: int | None = None, chat_id: int | None = None) -> dict:
    status = str(status or "").strip().casefold()
    if status not in {"draft", "active", "archived"}:
        raise SkillError("status must be draft, active, or archived")
    chat_id, user_id, _scope, _owner = _scope_context(
        chat_id=chat_id, user_id=user_id, use_context=user_id is None and chat_id is None,
    )
    with _store_lock:
        store = _load_store_locked()
        skill = _find_skill_locked(store, identifier, chat_id=chat_id, user_id=user_id, include_drafts=True, include_archived=True)
        if skill is None:
            raise SkillError("skill not found in the current scope")
        skill["status"] = status
        skill["updated_at"] = _utc_now_iso()
        _write_skill_filesystem(skill)
        _save_store_locked(store)
        return _public_skill(skill, include_content=False)


def record_skill_use(identifier: str, *, success: bool | None = None) -> None:
    chat_id, user_id, _scope, _owner = _scope_context()
    with _store_lock:
        store = _load_store_locked()
        skill = _find_skill_locked(store, identifier, chat_id=chat_id, user_id=user_id, include_drafts=False)
        if skill is None:
            return
        skill["last_used_at"] = _utc_now_iso()
        skill["use_count"] = int(skill.get("use_count", 0) or 0) + 1
        if success is True:
            skill["success_count"] = int(skill.get("success_count", 0) or 0) + 1
        elif success is False:
            skill["failure_count"] = int(skill.get("failure_count", 0) or 0) + 1
        _save_store_locked(store)


# Explicit-argument compatibility helpers are useful for scheduled jobs,
# migrations, and tests that do not run inside Telegram context variables.
def create_skill(*, name: str, description: str, instructions: str, user_id: int, scope: str = "private", chat_id: int | None = None, status: str = "active", **kwargs) -> dict:
    return save_skill(
        name, description, instructions, scope=scope, status=status,
        user_id=user_id, chat_id=chat_id, **kwargs,
    )


def update_skill(identifier: str, *, user_id: int, chat_id: int | None = None, description: str | None = None, instructions: str | None = None, **kwargs) -> dict:
    current = read_skill(identifier, user_id=user_id, chat_id=chat_id, include_drafts=True, include_archived=True)
    return save_skill(
        str(current.get("name") or identifier),
        str(description if description is not None else current.get("description") or current.get("name")),
        str(instructions if instructions is not None else current.get("procedure") or current.get("instructions") or ""),
        triggers=kwargs.pop("triggers", current.get("triggers")),
        prerequisites=kwargs.pop("prerequisites", current.get("prerequisites", "")),
        verification=kwargs.pop("verification", current.get("verification", "")),
        failure_modes=kwargs.pop("failure_modes", current.get("failure_modes", "")),
        tools=kwargs.pop("tools", current.get("tools")),
        scope=current.get("scope"), status=current.get("status", "draft"),
        user_id=user_id, chat_id=chat_id,
    )


def archive_skill(identifier: str, *, user_id: int, chat_id: int | None = None) -> dict:
    return set_skill_status(identifier, "archived", user_id=user_id, chat_id=chat_id)


async def skill_search(query: str, limit: int = MAX_RETRIEVED_SKILLS, user_id: int | None = None, chat_id: int | None = None) -> dict:
    try:
        limit = int(limit)
        if limit < 1 or limit > MAX_RETRIEVED_SKILLS * 3:
            raise SkillError(f"limit must be between 1 and {MAX_RETRIEVED_SKILLS * 3}")
        return {"ok": True, "results": search_skills(query, limit=limit, include_drafts=True, user_id=user_id, chat_id=chat_id)}
    except (SkillError, TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def skill_read(skill_id: str, user_id: int | None = None, chat_id: int | None = None) -> dict:
    try:
        return {"ok": True, "skill": read_skill(skill_id, include_drafts=True, include_archived=True, user_id=user_id, chat_id=chat_id)}
    except SkillError as exc:
        return {"ok": False, "error": str(exc)}


async def skill_view(
    skill_id: str,
    file_path: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> dict:
    """Hermes-style on-demand view of a skill or one supporting file."""
    try:
        skill = view_skill(
            skill_id,
            include_files=True,
            include_drafts=True,
            include_archived=True,
            user_id=user_id,
            chat_id=chat_id,
        )
        result = {"ok": True, "skill": skill}
        if file_path:
            result["file"] = {
                "path": _safe_relative_path(file_path),
                "content": read_skill_file(
                    file_path, identifier=skill_id, user_id=user_id, chat_id=chat_id,
                ),
            }
        return result
    except SkillError as exc:
        return {"ok": False, "error": str(exc)}


async def skill_save(
    name: str,
    description: str,
    procedure: str,
    triggers: Any = None,
    prerequisites: str = "",
    verification: str = "",
    failure_modes: str = "",
    tools: Any = None,
    scope: str | None = None,
    status: str = "draft",
    category: str | None = None,
    files: Mapping[str, Any] | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> dict:
    try:
        if SKILL_WRITE_APPROVAL:
            from emery.skill_approval import stage_skill_change
            return stage_skill_change(
                "create",
                skill_id=_slugify(name),
                after=procedure,
                payload={
                    "name": name, "description": description, "procedure": procedure,
                    "triggers": triggers, "prerequisites": prerequisites,
                    "verification": verification, "failure_modes": failure_modes,
                    "tools": tools, "scope": scope, "status": status,
                    "category": category, "files": files,
                },
                user_id=user_id,
                chat_id=chat_id,
            )
        skill = save_skill(
            name, description, procedure, triggers=triggers, prerequisites=prerequisites,
            verification=verification, failure_modes=failure_modes, tools=tools,
            scope=scope, status=status, category=category, files=files,
            user_id=user_id, chat_id=chat_id,
        )
        return {"ok": True, "skill": skill, "message": "Skill saved. Draft skills are not automatically applied until activated." if status == "draft" else "Skill saved and active."}
    except SkillError as exc:
        return {"ok": False, "error": str(exc)}


async def skill_list(include_drafts: bool = True, user_id: int | None = None, chat_id: int | None = None) -> dict:
    return {"ok": True, "skills": list_skills(include_drafts=include_drafts, user_id=user_id, chat_id=chat_id)}


async def skill_set_status(skill_id: str, status: str, user_id: int | None = None, chat_id: int | None = None) -> dict:
    try:
        if SKILL_WRITE_APPROVAL:
            from emery.skill_approval import stage_skill_change
            current = read_skill(
                skill_id, include_drafts=True, include_archived=True,
                user_id=user_id, chat_id=chat_id,
            )
            procedure = current.get("procedure") or current.get("instructions") or ""
            operation = "archive" if str(status).casefold() == "archived" else "update"
            return stage_skill_change(
                operation,
                skill_id=skill_id,
                before=procedure,
                after=procedure,
                payload={} if operation == "archive" else {"status": status},
                user_id=user_id,
                chat_id=chat_id,
            )
        return {"ok": True, "skill": set_skill_status(skill_id, status, user_id=user_id, chat_id=chat_id)}
    except SkillError as exc:
        return {"ok": False, "error": str(exc)}


async def skill_propose(
    name: str,
    description: str,
    instructions: str,
    *,
    user_id: int,
    scope: str = "private",
    chat_id: int | None = None,
    **kwargs,
) -> dict:
    """Compatibility alias for the explicit draft/proposal workflow."""
    try:
        skill = create_skill(
            name=name, description=description, instructions=instructions,
            user_id=user_id, scope=scope, chat_id=chat_id, status="draft", **kwargs,
        )
        return {"ok": True, "skill": skill}
    except SkillError as exc:
        return {"ok": False, "error": str(exc)}


__all__ = [
    "SkillError", "archive_skill", "create_skill", "list_skills", "read_skill", "record_skill_use", "retrieve_relevant_skills",
    "save_skill", "search_skills", "set_skill_status", "skill_list", "skill_read", "skill_save",
    "skill_search", "skill_set_status", "skill_propose", "skill_view", "update_skill", "format_relevant_skills",
    "skill_filesystem_root", "view_skill", "list_skill_files", "read_skill_file", "write_skill_file", "remove_skill_file",
    "migrate_json_skills_to_filesystem",
]
