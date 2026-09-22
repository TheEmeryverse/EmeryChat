"""Contract tests for Emery's durable, scoped skills.

These tests intentionally exercise the public ``emery.skills`` surface rather
than the on-disk representation.  A skill is procedural memory: it can be
retrieved later, but its visibility must still follow the current user/chat
scope.  The tests accept either synchronous or asynchronous implementations so
the storage layer can remain an implementation detail.
"""

import asyncio
import inspect
from collections.abc import Mapping

import pytest

from emery import skills


def _run(value):
    """Resolve a public API result without requiring pytest-asyncio."""
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def _field(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _skill_id(value):
    identifier = _field(value, "id") or _field(value, "skill_id")
    assert identifier, f"skill result did not expose an id: {value!r}"
    return identifier


def _items(value):
    """Extract result items while leaving envelope shape up to the API."""
    if isinstance(value, Mapping):
        if value.get("ok") is False:
            pytest.fail(f"skill operation failed: {value!r}")
        for key in ("results", "skills", "items"):
            if key in value:
                return list(value[key] or [])
        if "result" in value:
            result = value["result"]
            return list(result or []) if isinstance(result, (list, tuple)) else [result]
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _assert_denied(value):
    """Permission denials may be represented as None or a structured error."""
    if isinstance(value, Mapping):
        assert value.get("ok") is False or value.get("error") or value.get("status") in {
            "denied",
            "forbidden",
            "not_found",
        }, value
    else:
        assert value is None


def _assert_denied_call(callback):
    """Accept the two conventional public-API denial forms."""
    try:
        value = callback()
    except (PermissionError, KeyError, LookupError):
        return
    _assert_denied(value)


@pytest.fixture
def isolated_skill_store(tmp_path, monkeypatch):
    """Give each test a fresh durable store without inspecting its format."""
    path = tmp_path / "skills.json"
    # The constant follows Emery's existing memory/scratchpad configuration
    # pattern.  ``raising=False`` keeps this fixture compatible with an
    # implementation that exposes the path under a different internal name.
    monkeypatch.setattr(skills, "SKILLS_STORE_PATH", str(path), raising=False)
    return path


def _create(name, description, instructions, *, user_id, scope="private", chat_id=None):
    return _run(
        skills.create_skill(
            name=name,
            description=description,
            instructions=instructions,
            user_id=user_id,
            scope=scope,
            chat_id=chat_id,
        )
    )


def _read(skill_id, *, user_id, chat_id=None):
    return _run(skills.read_skill(skill_id, user_id=user_id, chat_id=chat_id))


def _search(query, *, user_id, chat_id=None, limit=8):
    return _run(
        skills.search_skills(
            query,
            user_id=user_id,
            chat_id=chat_id,
            limit=limit,
        )
    )


def test_skill_crud_persists_updates_and_archive_state(isolated_skill_store):
    created = _create(
        "release-checklist",
        "Safely prepare and verify a production release.",
        "Run tests, deploy the release, inspect health checks, and report rollback steps.",
        user_id=101,
    )
    skill_id = _skill_id(created)

    loaded = _read(skill_id, user_id=101)
    assert _field(loaded, "name") == "release-checklist"
    assert "health checks" in _field(loaded, "instructions", "")

    updated = _run(
        skills.update_skill(
            skill_id,
            user_id=101,
            description="Prepare, verify, and document a production release.",
            instructions="Run tests, deploy, verify health checks, and document rollback steps.",
        )
    )
    assert "document" in _field(updated, "description", "")
    assert "document rollback" in _field(updated, "instructions", "")

    archived = _run(skills.archive_skill(skill_id, user_id=101))
    assert str(_field(archived, "status", "")).lower() in {"archived", "inactive"}

    # A later read observes the durable archived state, rather than treating
    # archive as deletion or returning an earlier cached version.
    reread = _read(skill_id, user_id=101)
    assert str(_field(reread, "status", "")).lower() in {"archived", "inactive"}


def test_private_and_group_scopes_are_enforced_by_reads_and_search(isolated_skill_store):
    private = _create(
        "private-deploy-notes",
        "Personal deployment procedure.",
        "Use the private deployment host and confirm the owner before acting.",
        user_id=101,
    )
    group = _create(
        "team-oncall",
        "Shared on-call handoff procedure.",
        "Check the incident queue, assign an owner, and post the handoff.",
        user_id=101,
        scope="group",
        chat_id=-1001,
    )

    _assert_denied_call(lambda: _read(_skill_id(private), user_id=202))
    _assert_denied_call(lambda: _read(_skill_id(group), user_id=202, chat_id=-1002))

    same_user_private = _items(_search("deployment procedure", user_id=101))
    assert any(_skill_id(item) == _skill_id(private) for item in same_user_private)

    same_group = _items(_search("on-call handoff", user_id=202, chat_id=-1001))
    assert any(_skill_id(item) == _skill_id(group) for item in same_group)

    other_group = _items(_search("on-call handoff", user_id=202, chat_id=-1002))
    assert all(_skill_id(item) != _skill_id(group) for item in other_group)


def test_search_ranks_matching_skills_bounds_results_and_excludes_archived(isolated_skill_store):
    primary = _create(
        "database-migration",
        "Apply a database migration with a reversible rollback.",
        "Back up the database, apply the migration, verify schema health, and retain rollback SQL.",
        user_id=101,
    )
    _create(
        "weekly-report",
        "Prepare the recurring status report.",
        "Collect metrics and summarize notable changes for the team.",
        user_id=101,
    )
    archived = _create(
        "old-migration",
        "Deprecated database migration procedure.",
        "Do not use this procedure; it is retained only for historical reference.",
        user_id=101,
    )
    archived_id = _skill_id(archived)
    _run(skills.archive_skill(archived_id, user_id=101))

    results = _items(_search("database migration rollback", user_id=101, limit=1))
    assert len(results) <= 1
    assert results
    assert _skill_id(results[0]) == _skill_id(primary)
    assert all(_skill_id(item) != archived_id for item in _items(_search("database migration", user_id=101)))


def test_retrieval_returns_prompt_ready_skill_content_without_cross_scope_leaks(isolated_skill_store):
    created = _create(
        "home-status",
        "Check the home status in a consistent order.",
        "Inspect configured cameras, check the calendar, then summarize only notable events.",
        user_id=101,
    )
    skill_id = _skill_id(created)

    retrieved = _run(
        skills.retrieve_relevant_skills(
            "What is the usual home status check?",
            user_id=101,
            limit=3,
        )
    )
    items = _items(retrieved)
    assert items
    match = next(item for item in items if _skill_id(item) == skill_id)
    assert "configured cameras" in str(_field(match, "instructions", match))

    other_user = _run(
        skills.retrieve_relevant_skills(
            "home status check",
            user_id=202,
            limit=3,
        )
    )
    assert all(_skill_id(item) != skill_id for item in _items(other_user))


def test_model_facing_skill_handlers_use_stable_envelopes_and_preserve_scope(isolated_skill_store):
    proposed = _run(
        skills.skill_propose(
            name="incident-triage",
            description="Triage a new incident consistently.",
            instructions="Confirm impact, assign an owner, and record the next update time.",
            user_id=101,
            scope="private",
        )
    )
    assert proposed["ok"] is True
    skill_id = _skill_id(proposed.get("skill", proposed.get("result")))
    proposed_skill = proposed.get("skill", proposed.get("result"))
    assert str(_field(proposed_skill, "status", "")).lower() in {"draft", "proposed"}

    found = _run(
        skills.skill_search(
            query="incident triage",
            user_id=101,
            limit=5,
        )
    )
    assert found["ok"] is True
    assert any(_skill_id(item) == skill_id for item in _items(found))

    _assert_denied_call(lambda: _run(skills.skill_read(skill_id=skill_id, user_id=202)))

    invalid = _run(skills.skill_search(query="incident", user_id=101, limit=0))
    assert invalid["ok"] is False
    assert invalid.get("error")
