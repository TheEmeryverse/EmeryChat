from emery.config import ENABLE_DOCLING, ENABLE_MEMORY, REOLINK_CAMERAS, SKILL_WRITE_APPROVAL
from emery.memory import save_user_memory, get_camera_security_log
from emery.scratchpad import clear_scratchpad, jot_down_note, read_scratchpad
from emery.skills import (
    archive_skill,
    skill_list,
    skill_read,
    read_skill,
    skill_view,
    skill_save,
    skill_search,
    skill_set_status,
    set_skill_status as apply_skill_status,
    update_skill,
    write_skill_file,
    remove_skill_file,
)
from emery.skill_approval import stage_skill_change
import emery.globals as skill_globals
from emery.command_execution import run_command
from emery.terminal_tools import (
    terminal_exec,
    terminal_session_start,
    terminal_session_write,
    terminal_session_read,
    terminal_session_close,
    terminal_job_start,
    terminal_job_status,
    terminal_job_read,
    terminal_job_wait,
    terminal_job_cancel,
    terminal_list_sessions,
    terminal_list_jobs,
)
from emery.browser_control import (
    browser_click,
    browser_back,
    close_browser,
    close_browser_tab,
    browser_console,
    browser_handle_dialog,
    browser_navigate,
    browser_press,
    browser_scroll,
    browser_screenshot,
    browser_snapshot,
    browser_type,
    list_browser_tabs,
    open_browser_tab,
)
from emery.browser_session_tools import (
    browser_session_cleanup,
    browser_session_close,
    browser_session_list,
    browser_session_open_tab,
    browser_session_start,
    browser_session_status,
)

from emery.tools import (
    get_calendar_events,
    get_nest_thermostats, set_nest_thermostat_mode, set_nest_thermostat_temperature,
    overseer_search_movie, overseer_request_movie, overseer_search_tv, overseer_request_tv_season,
    get_noaa_weather, set_weather_location_alias, remove_weather_location_alias, list_weather_location_aliases,
    get_news_headlines,
    get_nasa_apod,
    get_today_in_history,
    web_search,
    generate_image,
    speak_message,
    get_system_stats,
    fetch_web_content, extract_document_with_docling, use_research_image, get_youtube_transcript,
    search_fred_series, get_fred_series_observations,
    search_imf_indicators, get_imf_datamapper_series,
    get_stock_snapshot, get_stock_price_history,
    get_bond_market_dashboard, get_inflation_dashboard,
    get_us_macro_dashboard, get_equity_market_dashboard, get_global_macro_dashboard,
    get_housing_consumer_dashboard, get_labor_market_dashboard,
    get_reolink_snapshot, get_available_cameras,
    delegate_to_coprocessor, react_to_message, reply_to_message,
    send_sticker, send_gif,
    list_portainer_environments, list_portainer_containers, update_portainer_container,
    import_recipe_to_mealie
)

# Helper to check if a feature is enabled
def is_enabled(var_name):
    try:
        import emery.config as config
        val = getattr(config, var_name, None)
        if val is not None:
            return bool(val) if isinstance(val, bool) else str(val).lower() == "true"
    except Exception:
        pass
    return False


AVAILABLE_TOOLS = {}
tools_schema = []


async def skill_manage(
    operation: str,
    skill_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    procedure: str | None = None,
    triggers=None,
    prerequisites: str = "",
    verification: str = "",
    failure_modes: str = "",
    tools=None,
    category: str | None = None,
    files=None,
    scope: str | None = None,
    status: str | None = None,
    approval: str | None = None,
    path: str | None = None,
    content: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> dict:
    """Hermes-shaped lifecycle adapter over the existing skill backend.

    ``create``, ``patch``/``update``, ``approve``, and ``archive``/``delete``
    are real operations delegated to :mod:`emery.skills`. Supporting-file
    paths are relative to the selected skill directory and are scope-checked
    by the backend.
    """
    operation = str(operation or "").strip().casefold()
    user_id = skill_globals.current_user_id.get() if user_id is None else user_id
    chat_id = skill_globals.TARGET_CHAT_ID.get() if chat_id is None else chat_id

    requested_status = str(status or "").strip().casefold()
    requested_approval = str(approval or "").strip().casefold()
    if requested_approval:
        if requested_approval not in {"draft", "approved"}:
            return {"ok": False, "error": "approval must be draft or approved"}
        requested_status = "active" if requested_approval == "approved" else "draft"
    if requested_status and requested_status not in {"draft", "active", "archived"}:
        return {"ok": False, "error": "status must be draft, active, or archived"}
    if requested_status == "active" and requested_approval != "approved" and not SKILL_WRITE_APPROVAL:
        return {"ok": False, "error": "activating a skill requires approval='approved'"}

    if SKILL_WRITE_APPROVAL and operation in {"patch", "update", "archive", "delete", "write_file", "remove_file"}:
        if not skill_id:
            return {"ok": False, "error": f"{operation} requires skill_id"}
        try:
            current = read_skill(skill_id, include_drafts=True, include_archived=True, user_id=user_id, chat_id=chat_id)
            current_procedure = current.get("procedure") or current.get("instructions") or ""
            if operation in {"patch", "update"}:
                payload = {
                    key: value for key, value in {
                        "description": description,
                        "instructions": procedure,
                        "triggers": triggers,
                        "prerequisites": prerequisites or None,
                        "verification": verification or None,
                        "failure_modes": failure_modes or None,
                        "tools": tools,
                    }.items() if value is not None
                }
                if requested_status:
                    payload["status"] = requested_status
                after = procedure if procedure is not None else current_procedure
                return stage_skill_change(
                    "update", skill_id=skill_id, before=current_procedure, after=after,
                    payload=payload, user_id=user_id, chat_id=chat_id,
                )
            if operation in {"archive", "delete"}:
                return stage_skill_change(
                    "archive", skill_id=skill_id, before=current_procedure,
                    summary=f"Archive skill {current.get('name') or skill_id}",
                    user_id=user_id, chat_id=chat_id,
                )
            if not path:
                return {"ok": False, "error": f"{operation} requires path"}
            if operation == "write_file" and content is None:
                return {"ok": False, "error": "write_file requires content"}
            return stage_skill_change(
                operation, skill_id=skill_id, target_path=path,
                after=content if operation == "write_file" else None,
                payload={}, user_id=user_id, chat_id=chat_id,
            )
        except (KeyError, LookupError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    try:
        if operation == "create":
            if not name or not description or not procedure:
                return {"ok": False, "error": "create requires name, description, and procedure"}
            result = await skill_save(
                name=name,
                description=description,
                procedure=procedure,
                triggers=triggers,
                prerequisites=prerequisites,
                verification=verification,
                failure_modes=failure_modes,
                tools=tools,
                category=category,
                files=files,
                scope=scope,
                status=requested_status or "draft",
                user_id=user_id,
                chat_id=chat_id,
            )
            return result

        if operation in {"write_file", "remove_file"}:
            if requested_approval != "approved":
                return {"ok": False, "error": f"{operation} requires approval='approved'"}
            if not skill_id or not path:
                return {"ok": False, "error": f"{operation} requires skill_id and path"}
            if operation == "write_file":
                if content is None:
                    return {"ok": False, "error": "write_file requires content"}
                return {"ok": True, "file": write_skill_file(
                    path, content, identifier=skill_id, user_id=user_id, chat_id=chat_id,
                )}
            return {"ok": True, "file": remove_skill_file(
                path, identifier=skill_id, user_id=user_id, chat_id=chat_id,
            )}

        if operation in {"patch", "update"}:
            if not skill_id:
                return {"ok": False, "error": "patch/update requires skill_id"}
            if description is None and procedure is None and triggers is None and not any(
                value for value in (prerequisites, verification, failure_modes, tools, requested_status)
            ):
                return {"ok": False, "error": "patch/update requires at least one skill field"}
            patch_kwargs = {}
            if description is not None:
                patch_kwargs["description"] = description
            if procedure is not None:
                patch_kwargs["instructions"] = procedure
            if triggers is not None:
                patch_kwargs["triggers"] = triggers
            if prerequisites:
                patch_kwargs["prerequisites"] = prerequisites
            if verification:
                patch_kwargs["verification"] = verification
            if failure_modes:
                patch_kwargs["failure_modes"] = failure_modes
            if tools is not None:
                patch_kwargs["tools"] = tools
            result = update_skill(skill_id, user_id=user_id, chat_id=chat_id, **patch_kwargs)
            if requested_status in {"draft", "active", "archived"}:
                result = apply_skill_status(skill_id, requested_status, user_id=user_id, chat_id=chat_id)
            return {"ok": True, "skill": result}

        if operation in {"approve", "activate"}:
            if not skill_id:
                return {"ok": False, "error": "approve requires skill_id"}
            return {"ok": True, "skill": apply_skill_status(skill_id, "active", user_id=user_id, chat_id=chat_id)}

        if operation in {"archive", "delete"}:
            if not skill_id:
                return {"ok": False, "error": "archive/delete requires skill_id"}
            return {"ok": True, "skill": archive_skill(skill_id, user_id=user_id, chat_id=chat_id)}

        return {"ok": False, "error": "operation must be create, patch, update, approve, activate, archive, delete, write_file, or remove_file"}
    except (KeyError, LookupError, TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


# --- General Scratchpad (always enabled) ---
AVAILABLE_TOOLS["jot_down_note"] = jot_down_note
AVAILABLE_TOOLS["read_scratchpad"] = read_scratchpad
AVAILABLE_TOOLS["clear_scratchpad"] = clear_scratchpad
tools_schema.extend([
    {
        "type": "function",
        "function": {
            "name": "jot_down_note",
            "description": (
                "Save one temporary working note for the current chat/thread. Use during multi-step work or research for confirmed facts, source takeaways, decisions, or unresolved questions that may be needed later. Read it with read_scratchpad. This is not durable personal memory: do not store secrets, sensitive personal facts, or every intermediate thought."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "One self-contained note with enough context to make sense later; record a fact, decision, source takeaway, or open question rather than raw chain-of-thought.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional short label such as 'Finding', 'Decision', or 'Open question'.",
                    },
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_scratchpad",
            "description": (
                "Read all temporary working notes for the current chat/thread. Use at the start or continuation of a long task when earlier research, decisions, or open questions may be outside the active context. This is temporary task state, not long-term personal memory."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_scratchpad",
            "description": (
                "Delete every temporary working note for the current chat/thread. Use only when the user explicitly asks to clear, reset, or forget the scratchpad. Do not clear it merely because a task is complete."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
])

# --- Durable procedural skills ---
AVAILABLE_TOOLS.update({
    "skill_search": skill_search,
    "skill_read": skill_read,
    "skill_view": skill_view,
    "skill_save": skill_save,
    "skill_list": skill_list,
    "skill_set_status": skill_set_status,
    "skill_manage": skill_manage,
})
tools_schema.extend([
    {
        "type": "function",
        "function": {
            "name": "skill_manage",
            "description": (
                "Manage the lifecycle of a durable procedural skill. Use operation='create' for a new skill, "
                "'patch' or 'update' for targeted metadata/procedure changes, 'approve' to activate an explicitly "
                "approved draft, and 'archive'/'delete' to disable it without erasing history. Use 'write_file' or "
                "'remove_file' for approved changes to references, templates, scripts, or assets. New or inferred "
                "skills must remain drafts unless the user explicitly approves activation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["create", "patch", "update", "approve", "activate", "archive", "delete", "write_file", "remove_file"],
                        "description": "Lifecycle operation to perform.",
                    },
                    "skill_id": {"type": "string", "description": "Existing skill ID, exact name, or slug for non-create operations."},
                    "name": {"type": "string", "description": "Short human-readable name for create."},
                    "description": {"type": "string", "description": "Purpose and matching context; required for create."},
                    "procedure": {"type": "string", "description": "Reusable procedure; required for create and optional for patch/update."},
                    "category": {"type": "string", "description": "Optional category directory for a new skill."},
                    "files": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Optional supporting files for create, keyed by references/, templates/, scripts/, or assets/ paths."},
                    "triggers": {"type": "array", "items": {"type": "string"}, "description": "Phrases or task descriptions that match this skill."},
                    "prerequisites": {"type": "string", "description": "Required inputs, configuration, or assumptions."},
                    "verification": {"type": "string", "description": "How to verify success before reporting completion."},
                    "failure_modes": {"type": "string", "description": "Known failures and safe recovery behavior."},
                    "tools": {"type": "array", "items": {"type": "string"}, "description": "Normal Emery tool names used by the procedure."},
                    "scope": {"type": "string", "enum": ["private", "group"], "description": "Skill scope for create; defaults from the active chat."},
                    "status": {"type": "string", "enum": ["draft", "active", "archived"], "description": "Requested lifecycle status. Prefer draft unless explicitly approved."},
                    "approval": {"type": "string", "enum": ["draft", "approved"], "description": "Conversational approval marker; approved maps to active."},
                    "path": {"type": "string", "description": "Relative supporting-file path for write_file/remove_file; absolute and traversal paths are invalid by contract."},
                    "content": {"type": "string", "description": "UTF-8 supporting-file content for write_file."},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_search",
            "description": "Search durable procedural skills relevant to the user's request. Skills are reusable playbooks, not permissions; normal tool schemas, privacy rules, approvals, and user instructions always win.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Plain-language task or goal to search for."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 9, "description": "Maximum number of matching skills."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_view",
            "description": "Progressively disclose one complete skill by ID, exact name, or slug. Automatic skill context is summary-only; call this before relying on the procedure, verification, or failure guidance. Drafts and archived skills can be viewed for review but are not automatically applied.",
            "parameters": {
                "type": "object",
                    "properties": {
                        "skill_id": {"type": "string", "description": "Skill ID, exact name, or slug."},
                        "file_path": {"type": "string", "description": "Optional relative supporting-file path such as references/api.md."},
                    },
                "required": ["skill_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_read",
            "description": "Read the complete durable skill identified by skill ID, name, or slug when the automatically supplied skill context is incomplete or you need its exact procedure.",
            "parameters": {
                "type": "object",
                "properties": {"skill_id": {"type": "string", "description": "Skill ID, exact name, or slug."}},
                "required": ["skill_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_save",
            "description": "Save a reusable procedural playbook in the current private or group scope. Use when the user explicitly asks Emery to remember how to do something, when the user approves saving a successful repeatable procedure, or to preserve a genuinely reusable successful workflow as a draft. Prefer status='draft' for inferred procedures; use status='active' only when the user clearly asks to make it available immediately. Do not store secrets or raw chain-of-thought.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human-readable skill name."},
                    "description": {"type": "string", "description": "What task this skill handles and when it is useful."},
                    "procedure": {"type": "string", "description": "Numbered, reusable steps. Reference normal Emery tool names; do not write executable code or claim permissions."},
                    "triggers": {"type": "array", "items": {"type": "string"}, "description": "Phrases or task descriptions that should match this skill."},
                    "prerequisites": {"type": "string", "description": "Required configuration, inputs, or assumptions."},
                    "verification": {"type": "string", "description": "How to verify the procedure succeeded before reporting success."},
                    "failure_modes": {"type": "string", "description": "Known failure cases and safe recovery behavior."},
                    "tools": {"type": "array", "items": {"type": "string"}, "description": "Normal Emery tool names used by the procedure."},
                    "category": {"type": "string", "description": "Optional category directory for the SKILL.md document."},
                    "files": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Optional supporting files keyed by references/, templates/, scripts/, or assets/ paths."},
                    "scope": {"type": "string", "enum": ["private", "group"], "description": "Defaults to private in a DM and group in a group chat."},
                    "status": {"type": "string", "enum": ["draft", "active"], "description": "Draft is stored for review; active is eligible for automatic retrieval."},
                },
                "required": ["name", "description", "procedure"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_list",
            "description": "List durable skills visible in the current private or group scope. Use when the user asks what Emery has learned or wants to review saved procedures.",
            "parameters": {
                "type": "object",
                "properties": {"include_drafts": {"type": "boolean", "description": "Include saved draft skills; defaults to true."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_set_status",
            "description": "Change a visible skill between draft, active, and archived. Archive when the user asks to forget or disable a learned procedure. Activate only with clear user intent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Skill ID, exact name, or slug."},
                    "status": {"type": "string", "enum": ["draft", "active", "archived"]},
                },
                "required": ["skill_id", "status"],
                "additionalProperties": False,
            },
        },
    },
])

if is_enabled("ENABLE_COMMAND_EXECUTION"):
    AVAILABLE_TOOLS["run_command"] = run_command
    tools_schema.append({
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run one bounded, non-interactive shell command and return its exit code and capped output. Use for concrete system or project work that needs a command-line tool. Do not use this to download or parse PDF, DOCX, or PPTX files; use extract_document_with_docling. The command runs with a configured working directory, closed stdin, and a timeout. Do not use for interactive programs, passwords, or commands that wait for a human prompt. Commands that write, delete, change services, or publish externally may pause for Telegram approval; denial or expiry means the command is not executed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The complete non-interactive shell command to run; do not rely on interactive prompts or stdin.",
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "Optional absolute path or path relative to the configured command directory; defaults to COMMAND_EXECUTION_CWD.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Optional timeout in seconds; bounded by COMMAND_EXECUTION_MAX_TIMEOUT_SECONDS.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Short reason this command is needed; include it when the command may trigger an approval check.",
                    },
                },
                "required": ["command"],
            },
        },
    })
    AVAILABLE_TOOLS.update({
        "terminal_exec": terminal_exec,
        "terminal_session_start": terminal_session_start,
        "terminal_session_write": terminal_session_write,
        "terminal_session_read": terminal_session_read,
        "terminal_session_close": terminal_session_close,
        "terminal_job_start": terminal_job_start,
        "terminal_job_status": terminal_job_status,
        "terminal_job_read": terminal_job_read,
        "terminal_job_wait": terminal_job_wait,
        "terminal_job_cancel": terminal_job_cancel,
        "terminal_list_sessions": terminal_list_sessions,
        "terminal_list_jobs": terminal_list_jobs,
    })
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "terminal_exec",
                "description": "Run one non-interactive shell command and wait for it to finish. Use for a command that should complete within one bounded timeout. Do not use this to download or parse PDF, DOCX, or PPTX files; use extract_document_with_docling. The command runs through Emery's terminal runtime, returns a request ID, exit status, working directory, and capped output, and may require approval when it changes system or project state. For a command that must keep running or needs shell state between calls, use terminal_job_start or terminal_session_start instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The complete non-interactive shell command to run. Do not include a command that waits for keyboard input."},
                        "working_directory": {"type": "string", "description": "Optional absolute path or path relative to Emery's configured command directory."},
                        "timeout_seconds": {"type": "number", "description": "Optional maximum runtime in seconds; Emery clamps it to the configured limit."},
                        "justification": {"type": "string", "description": "Optional short explanation of why this command is needed; useful when the command may trigger approval."},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_session_start",
                "description": "Start a persistent interactive bash session and return a session ID. Use when later commands need the same working directory, environment, or shell state. After starting, send commands with terminal_session_write and read their output with terminal_session_read; close it with terminal_session_close when finished. The session is scoped to this chat and expires when its idle or lifetime limit is reached.",
                "parameters": {"type": "object", "properties": {
                    "working_directory": {"type": "string", "description": "Optional absolute path or path relative to Emery's configured command directory."},
                    "idle_timeout_seconds": {"type": "number", "description": "Optional idle timeout in seconds; the session closes after no activity for this long."},
                    "max_lifetime_seconds": {"type": "number", "description": "Optional hard lifetime in seconds; the session cannot outlive this limit."},
                }},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_session_write",
                "description": "Send input to an existing persistent terminal session. Include a trailing newline to execute a command; without it, the text is only typed into the shell. Call terminal_session_read afterward to collect output. Use the exact session_id returned by terminal_session_start.",
                "parameters": {"type": "object", "properties": {
                    "session_id": {"type": "string", "description": "Exact session_id returned by terminal_session_start or terminal_list_sessions."},
                    "input": {"type": "string", "description": "Input to write; do not include secrets unless explicitly requested."},
                    "justification": {"type": "string", "description": "Optional short explanation for input that may trigger approval."},
                }, "required": ["session_id", "input"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_session_read",
                "description": "Read output currently buffered by a persistent terminal session. Use after terminal_session_write, and call again if the command is still producing output. This only reads output; it does not send input or wait indefinitely.",
                "parameters": {"type": "object", "properties": {
                    "session_id": {"type": "string", "description": "Exact session_id returned by terminal_session_start or terminal_list_sessions."},
                    "max_output_chars": {"type": "integer", "description": "Optional maximum number of output characters to return."},
                }, "required": ["session_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_session_close",
                "description": "Close a persistent terminal session and terminate its shell process. Use after the session is no longer needed or when a command must be stopped. This invalidates the session_id for future calls.",
                "parameters": {"type": "object", "properties": {
                    "session_id": {"type": "string", "description": "Exact session_id returned by terminal_session_start or terminal_list_sessions."},
                    "reason": {"type": "string", "description": "Optional reason for closing the session."},
                }, "required": ["session_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_job_start",
                "description": "Start a non-interactive shell command in the background and return a job_id immediately. Use for work that may outlast one tool call but does not need an interactive shell. Do not use this to download or parse PDF, DOCX, or PPTX files; use extract_document_with_docling. Follow up with terminal_job_status to check state, terminal_job_read for buffered output, terminal_job_wait to wait for completion, or terminal_job_cancel to stop it. The job is bounded by a maximum lifetime and may require approval.",
                "parameters": {"type": "object", "properties": {
                    "command": {"type": "string", "description": "The complete non-interactive shell command to run in the background."},
                    "working_directory": {"type": "string", "description": "Optional absolute path or path relative to Emery's configured command directory."},
                    "max_lifetime_seconds": {"type": "number", "description": "Optional maximum job lifetime in seconds; Emery clamps it to the configured limit."},
                    "justification": {"type": "string", "description": "Optional short explanation of why this background command is needed; useful when approval may be required."},
                }, "required": ["command"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_job_status",
                "description": "Check whether a background terminal job is queued, running, completed, failed, cancelled, or expired. Use the exact job_id returned by terminal_job_start. This does not return the job's full output; use terminal_job_read for that.",
                "parameters": {"type": "object", "properties": {"job_id": {"type": "string", "description": "Exact job_id returned by terminal_job_start or terminal_list_jobs."}}, "required": ["job_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_job_read",
                "description": "Read output currently buffered by a background terminal job. Use the exact job_id returned by terminal_job_start. If the job is still running, call again later or use terminal_job_wait; this tool does not stop the job.",
                "parameters": {"type": "object", "properties": {"job_id": {"type": "string", "description": "Exact job_id returned by terminal_job_start or terminal_list_jobs."}, "max_output_chars": {"type": "integer", "description": "Optional maximum number of output characters to return."}}, "required": ["job_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_job_wait",
                "description": "Wait for a background terminal job to finish, fail, cancel, or reach a bounded wait timeout. Use the exact job_id returned by terminal_job_start. A wait timeout does not cancel the job; check it again with terminal_job_status or terminal_job_read.",
                "parameters": {"type": "object", "properties": {"job_id": {"type": "string", "description": "Exact job_id returned by terminal_job_start or terminal_list_jobs."}, "timeout_seconds": {"type": "number", "description": "Optional maximum number of seconds to wait for this call."}}, "required": ["job_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_job_cancel",
                "description": "Request cancellation of a running background terminal job. Use only when the user wants the job stopped or it is no longer useful. This may require approval and does not undo changes the job already made. Use the exact job_id returned by terminal_job_start.",
                "parameters": {"type": "object", "properties": {"job_id": {"type": "string", "description": "Exact job_id returned by terminal_job_start or terminal_list_jobs."}, "justification": {"type": "string", "description": "Optional short explanation for cancelling the job; useful when approval may be required."}}, "required": ["job_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_list_sessions",
                "description": "List persistent terminal sessions visible to this chat, including their IDs and lifecycle status. Use when you need to recover a session_id or inspect existing interactive work. It does not create, modify, or close sessions.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal_list_jobs",
                "description": "List background terminal jobs visible to this chat, including their IDs and lifecycle status. Use when you need to recover a job_id or inspect existing background work. It does not create, cancel, or wait for jobs.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ])

if is_enabled("ENABLE_BROWSER"):
    AVAILABLE_TOOLS.update({
        "list_browser_tabs": list_browser_tabs,
        "open_browser_tab": open_browser_tab,
        "browser_snapshot": browser_snapshot,
        "browser_screenshot": browser_screenshot,
        "browser_navigate": browser_navigate,
        "browser_click": browser_click,
        "browser_type": browser_type,
        "browser_press": browser_press,
        "browser_back": browser_back,
        "browser_scroll": browser_scroll,
        "browser_console": browser_console,
        "browser_handle_dialog": browser_handle_dialog,
        "close_browser_tab": close_browser_tab,
        "close_browser": close_browser,
    })
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "browser_session_start",
                "description": "Start a logical browser session owned by this chat/thread/user and return a browser_session_id. Use this when browser work should be isolated from other chats or when you need a group of tabs with one lifecycle. Open tabs with browser_session_open_tab, inspect the session with browser_session_status, and close it with browser_session_close when finished.",
                "parameters": {"type": "object", "properties": {
                    "lease_seconds": {"type": "number", "description": "Optional maximum session lifetime in seconds."},
                    "idle_timeout_seconds": {"type": "number", "description": "Optional idle timeout in seconds; idle sessions can be cleaned up automatically."},
                }},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_session_status",
                "description": "Inspect one logical browser session, including its lifecycle state and the tabs owned by it. Use the exact browser_session_id returned by browser_session_start or browser_session_list. This is read-only.",
                "parameters": {"type": "object", "properties": {"browser_session_id": {"type": "string", "description": "Exact session ID returned by browser_session_start or browser_session_list."}}, "required": ["browser_session_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_session_list",
                "description": "List logical browser sessions owned by this chat/thread/user. Use when you need to recover a browser_session_id or see which isolated sessions are still open. This does not list individual tabs outside those sessions.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_session_open_tab",
                "description": "Open one absolute HTTP(S) URL in an existing logical browser session and attach the new tab to it. Use the resulting target_id with the ordinary browser_snapshot, browser_click, browser_type, and related tab tools. The URL is opened only; inspect the page before claiming that an action succeeded.",
                "parameters": {"type": "object", "properties": {
                    "browser_session_id": {"type": "string", "description": "Exact session ID returned by browser_session_start or browser_session_list."},
                    "url": {"type": "string", "description": "One absolute HTTP(S) URL; embedded credentials are not allowed."},
                }, "required": ["browser_session_id", "url"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_session_close",
                "description": "Close one logical browser session and, by default, its owned tabs. Use only when the user is finished with that isolated browser work or explicitly asks to close it. This invalidates the session and may close multiple tabs.",
                "parameters": {"type": "object", "properties": {
                    "browser_session_id": {"type": "string", "description": "Exact session ID returned by browser_session_start or browser_session_list."},
                    "close_tabs": {"type": "boolean", "description": "Whether to close tabs owned by the session; defaults to true. Set false only when the tabs must remain open independently."},
                }, "required": ["browser_session_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_session_cleanup",
                "description": "Clean up browser sessions that have expired or exceeded their idle timeout. Use for explicit housekeeping or after browser work is complete; it does not close active sessions that are still within their lease.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ])
    AVAILABLE_TOOLS.update({
        "browser_session_start": browser_session_start,
        "browser_session_status": browser_session_status,
        "browser_session_list": browser_session_list,
        "browser_session_open_tab": browser_session_open_tab,
        "browser_session_close": browser_session_close,
        "browser_session_cleanup": browser_session_cleanup,
    })
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "list_browser_tabs",
                "description": "List tabs currently exposed by Emery's configured Chromium DevTools endpoint and return each tab's target_id, title, and URL. Use this first when working with an existing browser, or when a target_id is unknown. This does not open, navigate, or modify a tab.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_browser_tab",
                "description": "Open one absolute HTTP(S) URL as a new tab in the configured Chromium browser and return its target_id. This only loads the URL; it does not log in, click, submit, or prove that any page action succeeded. Call browser_snapshot before interacting with the new tab.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Absolute http(s) URL to open; embedded credentials are not allowed."},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_snapshot",
                "description": "Inspect one Chromium tab and return readable page text plus visible interactive elements labeled with short refs such as @e1. Call this before clicking, typing, or pressing keys. Use only refs from the latest snapshot: navigation, clicks, typing, scrolling, and many page updates make old refs stale.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id from list_browser_tabs or open_browser_tab."},
                        "full": {"type": "boolean", "description": "Include full page text in addition to interactive elements; defaults to false."},
                        "max_chars": {"type": "integer", "minimum": 1000, "maximum": 30000, "description": "Maximum snapshot characters; defaults to 12000."},
                    },
                    "required": ["target_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_screenshot",
                "description": "Capture the current view of one Chromium tab and attach the screenshot to the model turn for visual inspection. Use when layout, images, visual state, or text not exposed by browser_snapshot matters. This does not click or change the page.",
                "parameters": {
                    "type": "object",
                    "properties": {"target_id": {"type": "string", "description": "Chromium target_id from list_browser_tabs or open_browser_tab."}},
                    "required": ["target_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": "Navigate an existing Chromium tab to one absolute HTTP(S) URL. Navigation replaces the page and invalidates all previous element refs, so call browser_snapshot afterward before any click or typing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id to navigate."},
                        "url": {"type": "string", "description": "Absolute http(s) URL; embedded credentials are not allowed."},
                    },
                    "required": ["target_id", "url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_click",
                "description": "Click one visible interactive element in a Chromium tab by its exact ref from the latest browser_snapshot. Do not guess refs or reuse refs after the page changes. Inspect the resulting page with browser_snapshot before taking another action; the click may trigger approval or a native dialog.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id containing the element."},
                        "ref": {"type": "string", "description": "Element ref such as @e1 from browser_snapshot."},
                    },
                    "required": ["target_id", "ref"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_type",
                "description": "Clear the selected visible input or contenteditable element and type new text into it. Use only an exact input ref from the latest browser_snapshot, then inspect the result or submit with browser_press if needed. This is a page interaction, not a password manager; do not include secrets unless the user explicitly requested that exact action.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id containing the element."},
                        "ref": {"type": "string", "description": "Input ref such as @e1 from browser_snapshot."},
                        "text": {"type": "string", "description": "Text to enter; maximum 4000 characters. Do not include passwords or secrets unless the user explicitly requested that exact action."},
                    },
                    "required": ["target_id", "ref", "text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_press",
                "description": "Press one supported keyboard key in the selected Chromium tab, typically after browser_type or browser_click. Use keys such as Enter, Tab, Escape, Backspace, Delete, an arrow key, or one character. Inspect the page afterward if the key changes navigation, focus, or content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id."},
                        "key": {"type": "string", "description": "Key to press."},
                    },
                    "required": ["target_id", "key"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_back",
                "description": "Go back one entry in a Chromium tab's navigation history. The page changes and prior element refs become invalid, so call browser_snapshot afterward.",
                "parameters": {"type": "object", "properties": {"target_id": {"type": "string", "description": "Chromium target_id."}}, "required": ["target_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_scroll",
                "description": "Scroll one Chromium page by a bounded amount in one direction. Use browser_snapshot afterward to inspect the new viewport and obtain fresh element refs; scrolling can make the previous visible refs unusable.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id."},
                        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                        "amount": {"type": "integer", "minimum": 1, "maximum": 5000},
                    },
                    "required": ["target_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_console",
                "description": "Read recent console messages and JavaScript exceptions observed in one Chromium tab. Use for debugging page behavior or failed interactions, not as proof that a user-facing action succeeded; verify visible results with browser_snapshot or browser_screenshot.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id."},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 200},
                        "clear": {"type": "boolean"},
                    },
                    "required": ["target_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_handle_dialog",
                "description": "Accept or dismiss a currently open native JavaScript alert, confirm, or prompt. Use only when a previous browser action returned awaiting_dialog, and choose based on the returned dialog text. After handling it, inspect the page again; accepting may submit or confirm a user-visible action.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": "string", "description": "Chromium target_id."},
                        "action": {"type": "string", "enum": ["accept", "dismiss"]},
                        "prompt_text": {"type": "string", "description": "Optional response for a prompt when accepting it."},
                    },
                    "required": ["target_id", "action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "close_browser_tab",
                "description": "Close one selected Chromium tab by target_id. This is a destructive browser action and may require approval; use only when the user asked to close it or the tab is clearly no longer needed. Do not confuse this with browser_back, which keeps the tab open.",
                "parameters": {
                    "type": "object",
                    "properties": {"target_id": {"type": "string", "description": "Chromium target_id from list_browser_tabs."}},
                    "required": ["target_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "close_browser",
                "description": "Close Emery-managed CDP browser sessions and terminate Chromium only if Emery launched that browser process. Use only for explicit browser shutdown or cleanup. An externally managed Chromium process is left running, but active Emery tabs may be closed.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ])

# --- Conditional Tool Registration ---
if is_enabled("ENABLE_CALENDAR"):
    AVAILABLE_TOOLS["get_calendar_events"] = get_calendar_events
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_calendar_events", 
            "description": "List today's events from the user's configured Google Calendars, ordered by start time. Use when the user asks what is on their calendar today or what appointments/events they have today. This tool does not accept a date; ask for clarification rather than implying it can retrieve an arbitrary date.",
            "parameters": {"type": "object", "properties": {}}
        }
    })

if is_enabled("ENABLE_NEST"):
    AVAILABLE_TOOLS["get_nest_thermostats"] = get_nest_thermostats
    AVAILABLE_TOOLS["set_nest_thermostat_mode"] = set_nest_thermostat_mode
    AVAILABLE_TOOLS["set_nest_thermostat_temperature"] = set_nest_thermostat_temperature
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_nest_thermostats",
                "description": "Read all configured Nest thermostats and their current state, including name, device ID, ambient temperature, humidity, mode, target setpoints, HVAC state, and available modes. Use for status questions; this tool does not change thermostat settings.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_nest_thermostat_mode",
                "description": "Change a Nest thermostat's operating mode. Use only when the user explicitly asks to heat, cool, use heat/cool range mode, or turn the thermostat off. Obtain the exact device ID from `get_nest_thermostats`; allowed modes are HEAT, COOL, HEATCOOL, and OFF.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The full device ID/resource name returned by get_nest_thermostats (e.g. enterprises/{project_id}/devices/{device_id})."
                        },
                        "mode": {
                            "type": "string",
                            "description": "The target operating mode: HEAT, COOL, HEATCOOL, or OFF."
                        }
                    },
                    "required": ["device_id", "mode"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_nest_thermostat_temperature",
                "description": "Change a Nest thermostat's target temperature. Use only when the user explicitly asks to change the setpoint. Pass Celsius values; convert a Fahrenheit request to Celsius. The tool reads the current mode: use `temp_celsius` for HEAT or COOL, and use `heat_temp_celsius` and/or `cool_temp_celsius` for HEATCOOL. It cannot set a temperature while the thermostat is OFF.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The full device ID/resource name returned by get_nest_thermostats (e.g. enterprises/{project_id}/devices/{device_id})."
                        },
                        "temp_celsius": {
                            "type": "number",
                            "description": "Target temperature in Celsius for the current HEAT or COOL mode; convert from Fahrenheit if needed."
                        },
                        "heat_temp_celsius": {
                            "type": "number",
                            "description": "Optional heat-side target in Celsius for HEATCOOL range mode."
                        },
                        "cool_temp_celsius": {
                            "type": "number",
                            "description": "Optional cool-side target in Celsius for HEATCOOL range mode."
                        }
                    },
                    "required": ["device_id"]
                }
            }
        }
    ])

if is_enabled("ENABLE_SEERR"):
    AVAILABLE_TOOLS.update({
        "overseer_search_movie": overseer_search_movie,
        "overseer_request_movie": overseer_request_movie,
        "overseer_search_tv": overseer_search_tv,
        "overseer_request_tv_season": overseer_request_tv_season
    })
    tools_schema.extend([
        {"type": "function", "function": {
            "name": "overseer_search_movie", 
            "description": "Search the user's media server for a movie by title. Use this FIRST when the user asks to add or request a movie, before calling `overseer_request_movie`. Pass only the movie title—do not add years, actors, or other filters. Present the numbered matches to the user and wait for them to select one; do not request a movie during the search step.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Movie title only; omit year, actors, and other search qualifiers."}}, "required": ["query"]}
        }},
        {"type": "function", "function": {
            "name": "overseer_request_movie", 
            "description": "Request one movie on the user's media server. Use only after the user selects a result from `overseer_search_movie`, and pass that result's TMDB ID. Do not guess an ID or call this merely because the user mentioned a movie; obtain confirmation when multiple matches exist.",
            "parameters": {"type": "object", "properties": {"tmdb_id": {"type": "integer", "description": "TMDB ID from the movie selected in `overseer_search_movie`."}}, "required": ["tmdb_id"]}
        }},
        {"type": "function", "function": {
            "name": "overseer_search_tv", 
            "description": "Search the user's media server for a TV show by title. Use this FIRST when the user asks to add or request a show, before calling `overseer_request_tv_season`. Pass only the show title. Present the numbered matches and wait for the user to select one; do not request a season during the search step.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "TV show title only; omit year, actors, and other search qualifiers."}}, "required": ["query"]}
        }},
        {"type": "function", "function": {
            "name": "overseer_request_tv_season", 
            "description": "Request a season of one TV show on the user's media server. Use only after the user selects a result from `overseer_search_tv`, and pass that result's TMDB ID plus the requested season number. Use season 0 only when the user asks for all seasons; otherwise pass the specific season number. Do not guess the ID or infer a season the user did not request.",
            "parameters": {
                "type": "object", 
                "properties": {
                    "tmdb_id": {"type": "integer", "description": "TMDB ID from the selected `overseer_search_tv` result."},
                    "season_number": {"type": "integer", "description": "The requested season number; use 0 for all seasons, otherwise a specific positive season number."}
                }, 
                "required": ["tmdb_id", "season_number"]
            }
        }}
    ])

if is_enabled("ENABLE_WEATHER"):
    AVAILABLE_TOOLS["get_noaa_weather"] = get_noaa_weather
    AVAILABLE_TOOLS["set_weather_location_alias"] = set_weather_location_alias
    AVAILABLE_TOOLS["remove_weather_location_alias"] = remove_weather_location_alias
    AVAILABLE_TOOLS["list_weather_location_aliases"] = list_weather_location_aliases
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_noaa_weather",
                "description": "Get current NOAA/NWS weather for a U.S. place, including a city, state, ZIP code, street address, or saved alias such as home or work. Use for forecasts, hourly conditions, or weather alerts. Prefer the location stated by the user; if none is stated, use a saved home alias only when available, otherwise ask for a place rather than inventing one.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "Optional U.S. city, state, ZIP code, street address, or saved weather alias such as home or work."
                        },
                        "timeframe": {
                            "type": "string",
                            "enum": ["forecast", "hourly"],
                            "description": "Use forecast for the standard multi-period forecast or hourly for the next several hourly periods."
                        },
                        "include_alerts": {
                            "type": "boolean",
                            "description": "Set true to include active NOAA/NWS alerts for the location; defaults to true."
                        }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_weather_location_alias",
                "description": "Save or replace a persistent weather location alias. Use only when the user explicitly asks to set, save, update, or change a named place such as home, work, school, or office. Resolve the natural-language location as supplied by the user; this changes future weather lookups and should not be done implicitly.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {
                            "type": "string",
                            "description": "Short alias to save, such as home, work, school, office, or cabin."
                        },
                        "location": {
                            "type": "string",
                            "description": "The place to resolve and save, such as 'Houston, TX' or '123 Main St, Dallas, TX'."
                        }
                    },
                    "required": ["alias", "location"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "remove_weather_location_alias",
                "description": "Delete one saved weather location alias. Use only when the user explicitly asks to clear, remove, or delete a named place such as home or work; do not remove aliases as cleanup or because a lookup failed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {
                            "type": "string",
                            "description": "The saved alias to remove."
                        }
                    },
                    "required": ["alias"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_weather_location_aliases",
                "description": "List all saved persistent weather location aliases and their resolved places. Use when the user asks which named weather locations are configured or wants to check a saved alias before changing it.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    ])

if is_enabled("ENABLE_NEWS"):
    AVAILABLE_TOOLS["get_news_headlines"] = get_news_headlines
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_news_headlines", 
            "description": "Fetch the latest headlines from the bot's configured RSS news feeds. Use for a current headline roundup or a quick 'what's in the news' request. Do not use for deep research, a specific article, or a topic that needs web search and source inspection.",
            "parameters": {}
        }
    })

if is_enabled("ENABLE_NASA"):
    AVAILABLE_TOOLS["get_nasa_apod"] = get_nasa_apod
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_nasa_apod", 
            "description": "Fetch NASA's Astronomy Picture of the Day for today, including its title, explanation, and media URL. Use when the user asks for NASA APOD, NASA's picture of the day, or today's astronomy image. Include the raw media URL in the final response when presenting the result; do not substitute an embed URL.",
            "parameters": {}
        }
    })

if is_enabled("ENABLE_HISTORY"):
    AVAILABLE_TOOLS["get_today_in_history"] = get_today_in_history
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_today_in_history", 
            "description": "Fetch notable historical events, births, and deaths associated with today's calendar date. Use for questions such as 'what happened on this day in history?' or 'who was born today'; it is not a general historical search for another date or topic.",
            "parameters": {}
        }
    })

if is_enabled("ENABLE_SEARCH"):
    AVAILABLE_TOOLS["web_search"] = web_search
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "web_search", 
            "description": "Search the public web for current, unfamiliar, or source-based information when no more specific tool applies. Use one focused query first and at most one different follow-up if results are empty or genuinely contradictory. If the user supplied a URL, use fetch_web_content instead; for structured market or economic data, use the relevant finance tool. Do not repeat the same query or put raw result URLs in the final answer unless the user asks.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "One focused search query containing the topic, entity, and important constraint or date."}}, "required": ["query"]}
        }
    })

if is_enabled("ENABLE_IMAGEGEN"):
    AVAILABLE_TOOLS["generate_image"] = generate_image
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "generate_image", 
            "description": "This is the ONLY tool for generating, creating, drawing, illustrating, or designing a new image. When the user asks for a new image, use this tool and do not use terminal, browser, web-search, research-image, or any other tool instead. Do not use it for ordinary text descriptions, image analysis, or finding an existing image. Preserve the requested subject, style, composition, and constraints while making the prompt self-contained. After the tool returns, include its exact image prompt in the final response under an 'Image prompt:' label; do not paraphrase it.",
            "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "Self-contained visual instructions including subject, setting, style, composition, aspect ratio if relevant, and constraints."}}, "required": ["prompt"]}
        }
    })

if is_enabled("ENABLE_VOICE"):
    AVAILABLE_TOOLS["speak_message"] = speak_message
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "speak_message", 
            "description": "Convert a spoken script to audio and send one voice memo. Use only when the user's most recent message explicitly asks to speak, say something aloud, or send a voice message; do not use for an ordinary written answer. Pass natural conversational prose only—no Markdown, headings, lists, labels, emojis, or symbols.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The spoken voice memo script only. No markdown, headings, titles, bullets, numbered lists, section labels, emojis, or symbols."
                    }
                },
                "required": ["text"]
            }
        }
    })

if is_enabled("ENABLE_SYSTEM_STATS"):
    AVAILABLE_TOOLS["get_system_stats"] = get_system_stats
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_system_stats", 
            "description": "Read the current CPU and RAM utilization of the Emery host. Use for questions about this bot's current resource usage or whether the host is under load; this is not a general system diagnostic or historical metrics tool.",
            "parameters": {}
        }
    })

if is_enabled("ENABLE_WEB_SCRAPING"):
    AVAILABLE_TOOLS["fetch_web_content"] = fetch_web_content
    AVAILABLE_TOOLS["use_research_image"] = use_research_image
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "fetch_web_content", 
            "description": "Read and extract the main text from one specific public webpage URL. Use extract_document_with_docling instead for PDF, DOCX, or PPTX URLs. If the user supplied a normal webpage URL, use this directly; otherwise fetch at most one promising result after web_search unless the user asks for comparison or deep research. Do not use this to discover pages or fetch the same URL twice in one turn. The result includes the page title, resolved URL, extracted text, and image-candidate metadata; images are not downloaded or sent automatically.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Exactly one HTTP or HTTPS URL to read."}}, "required": ["url"]}
        }
    })
    if is_enabled("ENABLE_DOCLING"):
        AVAILABLE_TOOLS["extract_document_with_docling"] = extract_document_with_docling
        tools_schema.append({
            "type": "function",
            "function": {
                "name": "extract_document_with_docling",
                "description": (
                    "DEDICATED DOCLING PIPELINE: extract text from exactly one PDF, DOCX, or PPTX URL. "
                    "Use this first whenever the user asks Emery to read, inspect, summarize, or analyze a document URL. "
                    "Do not use run_command, terminal tools, curl, wget, Python, or browser tools to download or parse the document; those are for system or interactive web work, not document extraction. "
                    "Pass the user's actual question or focus when available so the pipeline can prioritize relevant pages and inspect visual details such as colors, maps, charts, and shaded tables. "
                    "The result contains Docling's structured, page-aware extraction plus a visual fallback when text alone may be insufficient, or an explicit extraction failure."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Exactly one HTTP or HTTPS URL for a PDF, DOCX, or PPTX document."},
                        "max_chars": {"type": "integer", "minimum": 1000, "maximum": 30000, "description": "Maximum extracted characters to return; defaults to 12000."},
                        "question": {"type": "string", "description": "The user's document question or focus. Include it when available so relevant pages and visual details receive priority."},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        })
    tools_schema.append({
        "type": "function",
        "function": {
            "name": "use_research_image",
            "description": "Act on one image candidate returned by fetch_web_content. Images are optional: do not call this just because a page contains an image. Use inspect for OCR/visual verification, attach when the main model needs to see the pixels, send when the user should receive the image, or inspect_and_send for both. Prefer one image and never exceed the enforced per-turn maximum of two; attach alone does not send anything to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string", "description": "Candidate ID returned by fetch_web_content, such as research_img_abc123."},
                    "action": {"type": "string", "enum": ["inspect", "attach", "send", "inspect_and_send"], "description": "Choose inspect for OCR/visual analysis, attach to show the pixels to the main model, send to deliver the image to Telegram, or inspect_and_send for both analysis and delivery."},
                    "question": {"type": "string", "description": "Optional focused question to answer from the image during inspect or inspect_and_send."},
                    "caption": {"type": "string", "description": "Optional concise Telegram caption for send or inspect_and_send; source attribution is added automatically."},
                },
                "required": ["image_id", "action"],
            },
        },
    })

if is_enabled("ENABLE_YOUTUBE_TRANSCRIPT"):
    AVAILABLE_TOOLS["get_youtube_transcript"] = get_youtube_transcript
    tools_schema.append({
        "type": "function",
        "function": {
            "name": "get_youtube_transcript",
            "description": "Retrieve the available captions/transcript for one YouTube video. Use when the user asks to summarize, quote, analyze, search within, or otherwise work from a video's spoken content. Accept a YouTube URL or exact 11-character video ID; do not guess an ID. This works only when public manual or auto-generated captions are available, and is not a general video-metadata or web-search tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_url_or_id": {
                        "type": "string",
                        "description": "A YouTube watch/shorts/embed/youtu.be URL or raw 11-character video ID."
                    },
                    "languages": {
                        "type": "string",
                        "description": "Optional comma-separated preferred transcript language codes, such as 'en' or 'en,es'. Defaults to English."
                    },
                    "translate_to": {
                        "type": "string",
                        "description": "Optional target language code for YouTube's caption translation, such as 'en'. Leave blank to keep the transcript's original selected language."
                    },
                    "include_timestamps": {
                        "type": "boolean",
                        "description": "Whether to include timestamps before each transcript segment. Use true when the user asks for timestamps or exact locations."
                    }
                },
                "required": ["video_url_or_id"]
            }
        }
    })

if is_enabled("ENABLE_FINANCE"):
    AVAILABLE_TOOLS.update({
        "search_fred_series": search_fred_series,
        "get_fred_series_observations": get_fred_series_observations,
        "search_imf_indicators": search_imf_indicators,
        "get_imf_datamapper_series": get_imf_datamapper_series,
        "get_stock_snapshot": get_stock_snapshot,
        "get_stock_price_history": get_stock_price_history,
        "get_bond_market_dashboard": get_bond_market_dashboard,
        "get_inflation_dashboard": get_inflation_dashboard,
        "get_us_macro_dashboard": get_us_macro_dashboard,
        "get_equity_market_dashboard": get_equity_market_dashboard,
        "get_global_macro_dashboard": get_global_macro_dashboard,
        "get_housing_consumer_dashboard": get_housing_consumer_dashboard,
        "get_labor_market_dashboard": get_labor_market_dashboard,
    })
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "search_fred_series",
                "description": "Discover the correct FRED series ID from a topic or keyword. Use this FIRST when the user asks for a specific economic indicator but has not supplied its FRED ID, such as 'core CPI', 'unemployment', 'real GDP', or 'the 2-year Treasury yield'. Inspect the returned titles, frequency, and units, then call `get_fred_series_observations` with the best matching ID. Do not use this when the user already gave an exact FRED series ID or when a high-level dashboard directly answers a broad question.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "A topic or indicator phrase, not necessarily an ID, such as 'core CPI', 'unemployment rate', 'real GDP', or '2 year treasury yield'."},
                        "limit": {"type": "integer", "description": "Optional number of candidate series to return; maximum 12."}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_fred_series_observations",
                "description": "Retrieve observations and metadata for one known FRED series. Use this when the user supplied an exact FRED series ID or after `search_fred_series` identified the correct ID. It returns the series title, frequency, units, latest value, and recent observations, with optional date bounds, unit transformation, frequency aggregation, and row limit. If you do not know the exact ID, use `search_fred_series` first; for broad multi-indicator questions, prefer a dashboard.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "series_id": {"type": "string", "description": "The exact FRED series ID, such as CPIAUCSL, UNRATE, FEDFUNDS, GDPC1, or DGS10."},
                        "observation_start": {"type": "string", "description": "Optional inclusive start date in YYYY-MM-DD format."},
                        "observation_end": {"type": "string", "description": "Optional inclusive end date in YYYY-MM-DD format."},
                        "units": {"type": "string", "description": "Optional FRED transformation such as lin (level), chg (change), pch (percent change), or pc1 (percent change from one year ago). Defaults to lin."},
                        "frequency": {"type": "string", "description": "Optional aggregation frequency: d (daily), w (weekly), bw (biweekly), m (monthly), q (quarterly), or a (annual)."},
                        "limit": {"type": "integer", "description": "Optional number of observations to return; maximum 24. Results are newest first."}
                    },
                    "required": ["series_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_imf_indicators",
                "description": "Discover the correct IMF DataMapper indicator code from a concept or keyword. Use this FIRST for a specific IMF or cross-country economic measure when the user has not supplied the code, such as real GDP growth, inflation, government debt, or the current account. Inspect the returned labels and descriptions, then call `get_imf_datamapper_series` with the best code. Do not use this when the user already gave an exact IMF indicator code or when a broad dashboard is sufficient.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "A concept or keyword, such as 'real GDP growth', 'inflation', 'government debt', or 'current account'."},
                        "limit": {"type": "integer", "description": "Optional number of candidate indicators to return; maximum 12."}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_imf_datamapper_series",
                "description": "Retrieve one known IMF DataMapper indicator for one or more countries across years. Use this when the user supplied an exact indicator code or after `search_imf_indicators` identified it. If you do not know the code, discover it first instead of guessing. For broad cross-country questions covering several standard measures, prefer `get_global_macro_dashboard`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "indicator": {"type": "string", "description": "The exact IMF indicator code, such as NGDP_RPCH, PCPIPCH, or GGXWDG_NGDP."},
                        "countries": {"type": "string", "description": "Optional comma-separated ISO-3 country codes such as USA,CAN,MEX. Defaults to USA."},
                        "start_year": {"type": "integer", "description": "Optional first year of the comparison window."},
                        "end_year": {"type": "integer", "description": "Optional last year of the comparison window."}
                    },
                    "required": ["indicator"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_stock_snapshot",
                "description": "Get a current stock or ETF snapshot plus basic fundamentals. Use for a ticker's current price, day range, previous close, 52-week range, market cap, EBITDA, P/E, EPS, beta, business summary, or recent quarterly earnings. If the user wants multiple daily prices, a chart-like time sequence, or OHLCV history, use `get_stock_price_history` instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol such as AAPL, MSFT, BRK.B, or SPY."}
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_stock_price_history",
                "description": "Get recent historical daily OHLCV data for one stock or ETF ticker. Use when the user asks about price action over time, a recent trading range, multiple daily closes, or daily open/high/low/close/volume rows. This is daily history, not an intraday quote. For the current quote, valuation, fundamentals, or earnings context, use `get_stock_snapshot` instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol such as AAPL, MSFT, BRK.B, or SPY."},
                        "outputsize": {"type": "string", "description": "Optional. Use 'compact' for recent history or 'full' when the requested dates may be older."},
                        "limit": {"type": "integer", "description": "Optional number of newest daily rows to return; maximum 30."}
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_bond_market_dashboard",
                "description": "Get a curated bond-market dashboard. Use this FIRST for broad questions about bonds, Treasury yields, the yield curve, mortgage rates, credit spreads, inflation expectations, or how rates relate to policy, growth, labor, and equities. It bundles the relevant FRED series; do not use it when the user asks for one exact FRED series ID, which should use `get_fred_series_observations`.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_inflation_dashboard",
                "description": "Get a curated inflation dashboard covering headline and core CPI, headline and core PCE, and market-based inflation expectations. Use this FIRST for broad questions about inflation, disinflation, price pressures, or inflation expectations. If the user asks for one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_us_macro_dashboard",
                "description": "Get a curated U.S. macroeconomic dashboard covering real GDP, unemployment, payrolls, retail sales, industrial production, the Fed funds rate, and the 10-year Treasury yield. Use this FIRST for broad questions about the U.S. economy, growth, labor, activity, recession risk, or policy context. For one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_equity_market_dashboard",
                "description": "Get a curated broad equity-market dashboard covering the S&P 500, Nasdaq, VIX, Treasury yields, high-yield credit spreads, and the dollar. Use this FIRST for questions about the overall stock market, market performance, risk sentiment, or cross-asset conditions. If the user names a specific stock or ETF ticker, use `get_stock_snapshot` or `get_stock_price_history` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_global_macro_dashboard",
                "description": "Get a curated IMF-based global macro dashboard comparing real GDP growth, inflation, unemployment, government debt, and current-account balances across countries. Use this FIRST for broad cross-country or global-economy questions. Optional countries are comma-separated ISO-3 codes; use `get_imf_datamapper_series` instead for one exact IMF indicator or a custom single-measure comparison.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "countries": {"type": "string", "description": "Optional comma-separated ISO-3 country or area codes. Defaults to USA,CHN,EAQ,JPN,GBR,IND."},
                        "start_year": {"type": "integer", "description": "Optional first year of the comparison window; defaults to 2022."},
                        "end_year": {"type": "integer", "description": "Optional last year of the comparison window; defaults to the current year."}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_housing_consumer_dashboard",
                "description": "Get a curated housing-and-consumer dashboard covering mortgage rates, home prices, housing starts, building permits, consumer spending, consumer credit, and delinquency stress. Use this FIRST for broad questions about housing, affordability, construction, household spending, credit, or consumer health. For one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_labor_market_dashboard",
                "description": "Get a curated labor-market dashboard covering unemployment, payrolls, initial and continuing claims, job openings, quits, participation, employment utilization, and wage growth. Use this FIRST for broad questions about jobs, layoffs, hiring, labor supply, wage growth, or labor-market conditions. For one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        }
    ])

if is_enabled("ENABLE_REOLINK"):
    AVAILABLE_TOOLS["get_reolink_snapshot"] = get_reolink_snapshot
    AVAILABLE_TOOLS["get_available_cameras"] = get_available_cameras
    AVAILABLE_TOOLS["get_camera_security_log"] = get_camera_security_log
    
    # Extract camera names from configuration
    camera_names = list(REOLINK_CAMERAS.keys())
            
    camera_list_str = ", ".join([f"'{c}'" for c in camera_names]) if camera_names else "'front', 'frontdoor'"
    
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_reolink_snapshot",
                "description": "Capture a live snapshot from one configured Reolink security camera and return an AI scene/threat analysis. Use when the user asks to check, look at, view, or patrol a specific camera. This is for the current live scene; use `get_camera_security_log` for past activity and `get_available_cameras` when the camera name is unknown.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": f"The configured camera name to check. Choose exactly one option from this list: {camera_list_str}. Do not invent a camera name; call `get_available_cameras` first if the user did not identify one."
                        }
                    },
                    "required": ["camera_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_available_cameras",
                "description": "List the configured and currently reachable home security camera names. Use when the user asks what cameras or feeds are available, or before a snapshot when the requested camera name is ambiguous or unknown. This does not capture a live image.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_camera_security_log",
                "description": "Read recent recorded security-camera activity, including AI threat reports and scene descriptions. Use when the user asks what happened, what was detected, or wants a recent security summary. This is historical log data, not a live camera snapshot; omit `camera_name` to review all cameras.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": "Optional configured camera name (for example, frontdoor) to filter by. Omit to include all cameras."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Optional maximum number of recent log entries; defaults to 10."
                        }
                    }
                }
            }
        }
    ])

if ENABLE_MEMORY:
    AVAILABLE_TOOLS["save_user_memory"] = save_user_memory
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "save_user_memory", 
            "description": "Persist one durable, future-relevant fact for later conversations. Use only for stable preferences, recurring constraints, names or relationships, household facts, long-term projects, owned devices/services, or standing instructions that should survive after chat history is cleared. Do not save temporary context, one-off updates, jokes, facts already stored, secrets, or sensitive private information in a group chat unless clearly appropriate and explicitly requested.",
            "parameters": {
                "type": "object", 
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "One concise, self-contained factual statement with no filler or commentary, such as 'Hudson prefers tabs over spaces in code editors.'"
                    }
                }, 
                "required": ["fact"]
            }
        }
    })

if is_enabled("ENABLE_PORTAINER"):
    AVAILABLE_TOOLS["list_portainer_environments"] = list_portainer_environments
    AVAILABLE_TOOLS["list_portainer_containers"] = list_portainer_containers
    AVAILABLE_TOOLS["update_portainer_container"] = update_portainer_container
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "list_portainer_environments",
                "description": "List Portainer environments with their names, IDs, types, and online/offline status. Use this read-only tool first when you need to identify an environment before listing or updating its containers.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_portainer_containers",
                "description": "List all running and stopped Docker containers in one Portainer environment, including container name, state, and image. Use after identifying the exact environment name; this tool is read-only and does not start, stop, or update containers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "environment_name": {
                            "type": "string",
                            "description": "The exact Portainer environment name, obtained from `list_portainer_environments` (for example, emeryverse or thegrand)."
                        }
                    },
                    "required": ["environment_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "update_portainer_container",
                "description": "Pull the latest image, stop, delete, recreate, and start one Docker container in Portainer while preserving its inspected configuration. This is a powerful, disruptive administrative action. Use only when the user explicitly asks to update, restart, recreate, or upgrade that specific container; never infer authorization from a status question or a general request to inspect containers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "environment_name": {
                            "type": "string",
                            "description": "The exact Portainer environment name, obtained from `list_portainer_environments`."
                        },
                        "container_name": {
                            "type": "string",
                            "description": "The exact container name to recreate, obtained from `list_portainer_containers` (for example, seerr or plex)."
                        }
                    },
                    "required": ["environment_name", "container_name"]
                }
            }
        }
    ])

if is_enabled("ENABLE_MEALIE"):
    AVAILABLE_TOOLS["import_recipe_to_mealie"] = import_recipe_to_mealie
    tools_schema.append({
        "type": "function",
        "function": {
            "name": "import_recipe_to_mealie",
            "description": "Import one recipe from a web URL into the user's Mealie recipe collection. Use when the user shares a recipe link or explicitly asks to save/import a recipe. Pass exactly one HTTP or HTTPS recipe URL; do not use for general webpage summaries or multiple links in one call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Exactly one HTTP or HTTPS URL for the recipe to import."
                    }
                },
                "required": ["url"]
            }
        }
    })


if is_enabled("ENABLE_SCHEDULER"):
    from emery.scheduler import add_scheduled_job, list_scheduled_jobs, remove_scheduled_job
    AVAILABLE_TOOLS.update({
        "add_scheduled_job": add_scheduled_job,
        "list_scheduled_jobs": list_scheduled_jobs,
        "remove_scheduled_job": remove_scheduled_job
    })
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "add_scheduled_job",
                "description": "Create one future reminder, recurring reminder, routine, monitor, or automated check. Use only when the user explicitly asks to schedule, remind, repeat, monitor, check later, or automate something; never create a job proactively. For a one-off calendar date without a time, ask for the time first. Put the complete action or reminder content in `prompt`, not only in the short label.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_type": {
                            "type": "string",
                            "enum": ["daily", "interval", "once", "weekly", "monthly", "yearly"],
                            "description": "Trigger type: daily, interval, once, weekly, monthly, or yearly. Use once for a one-off reminder; use a recurring type for repeated reminders or routines. Personal recurring reminders still use their recurring schedule type and are routed privately when target_user/wording indicates that they are personal."
                        },
                        "schedule_value": {
                            "type": "string",
                            "description": "Trigger value: daily requires HH:MM in 24-hour time; interval requires a duration such as 30m, 1h, or 3600 seconds; once requires localized YYYY-MM-DD HH:MM:SS or a relative delay such as 15m; weekly requires <day_name> <HH:MM>; monthly requires <day_of_month> <HH:MM>; yearly requires <MM-DD> <HH:MM>. Do not pass a date-only value for once."
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The complete instruction the bot will execute when triggered, including the actual reminder content or the tool/action to perform. Do not use a vague label such as 'send reminder about groceries'."
                        },
                        "description": {
                            "type": "string",
                            "description": "A short user-facing label for the job, such as Daily Weather Briefing. Keep actionable details in prompt."
                        },
                        "target_user": {
                            "type": "string",
                            "description": "Optional person or audience: a family member name/alias, me, us, or both. In group chats, me routes a personal reminder to the asker; us/both routes a shared reminder to the group topic."
                        },
                        "route_to_routines": {
                            "type": "boolean",
                            "description": "Optional. Set true for shared recurring briefings, checks, monitoring, or automation that should go to the routines topic. Leave false for personal reminders; routing otherwise follows target_user and wording."
                        }
                    },
                    "required": ["schedule_type", "schedule_value", "prompt", "description"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_scheduled_jobs",
                "description": "List currently configured scheduled jobs with their IDs, schedules, labels, prompts, and routing details. Use when the user asks what is scheduled or wants to inspect an existing reminder/routine before changing or removing it. This tool does not modify jobs.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "remove_scheduled_job",
                "description": "Cancel and delete one existing scheduled job by ID. Use only when the user clearly asks to cancel, stop, delete, or remove that reminder/routine. List jobs first if the correct ID is not already known; do not remove jobs merely because they are complete or unfamiliar.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "The exact unique job ID returned by `list_scheduled_jobs`."
                        }
                    },
                    "required": ["job_id"]
                }
            }
        }
    ])

AVAILABLE_TOOLS["delegate_to_coprocessor"] = delegate_to_coprocessor
tools_schema.append({
    "type": "function",
    "function": {
        "name": "delegate_to_coprocessor",
        "description": "Delegate long or mechanical text-only processing to the fast coprocessor. Use for summarization, extraction, classification, cleanup, formatting, or document parsing when the source is roughly over 1,500 characters, highly repetitive, or expensive to process inline. Do not use for ordinary conversation, direct factual answers, tasks requiring another tool, or work that needs independent reasoning rather than text transformation.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_prompt": {
                    "type": "string",
                    "description": "The exact text-processing instruction, such as 'Extract all dates and times' or 'Summarize this page'."
                },
                "content_to_process": {
                    "type": "string",
                    "description": "The complete text, CSV, email, transcript, or webpage content to process. Do not pass a URL alone when the task requires fetching it first."
                }
            },
            "required": ["task_prompt", "content_to_process"]
        }
    }
})

AVAILABLE_TOOLS["react_to_message"] = react_to_message
tools_schema.append({
    "type": "function",
    "function": {
        "name": "react_to_message",
        "description": "Add one lightweight Telegram emoji reaction to a chat message. Use when a simple reaction is natural, either instead of a response when no text is needed or as a small addition to text. Do not use it instead of answering a substantive question or completing requested work; use the optional message ID only when reacting to an older message.",
        "parameters": {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "One supported Telegram reaction emoji: 👍, 👎, ❤️, 🔥, 👏, 😂, 😮, 😢, 🎉, 🤔, or 👀."
                },
                "message_id": {
                    "type": "integer",
                    "description": "Optional ID of the message to react to. If omitted, the tool targets the latest user message in the current history."
                }
            },
            "required": ["emoji"]
        }
    }
})

AVAILABLE_TOOLS["reply_to_message"] = reply_to_message
tools_schema.append({
    "type": "function",
    "function": {
        "name": "reply_to_message",
        "description": "Make the bot's final response a Telegram reply to one specific earlier message. Use only when the user explicitly refers to that older message or threading materially clarifies the response. Do not use for normal back-and-forth conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": "The exact earlier message ID to quote/reply to."
                }
            },
            "required": ["message_id"]
        }
    }
})

AVAILABLE_TOOLS["send_sticker"] = send_sticker
tools_schema.append({
    "type": "function",
    "function": {
        "name": "send_sticker",
        "description": "Send one Telegram sticker to the current chat. Use when the user asks for a sticker or when a lightweight sticker response is natural. Pass a supported emoji to look up a sticker in the learned library, or pass a direct Telegram sticker file ID; this sends media and is not an emoji reaction.",
        "parameters": {
            "type": "object",
            "properties": {
                "sticker_id_or_emoji": {
                    "type": "string",
                    "description": "A supported lookup emoji such as 👍, ❤️, or 🔥, or a direct Telegram sticker file ID."
                }
            },
            "required": ["sticker_id_or_emoji"]
        }
    }
})

AVAILABLE_TOOLS["send_gif"] = send_gif
tools_schema.append({
    "type": "function",
    "function": {
        "name": "send_gif",
        "description": "Send one animated GIF to the current chat. Use when the user asks for a GIF or when a contextual lightweight animation is natural. Pass a direct HTTP(S) GIF/video URL or a short search query; this sends media and is not a web-search request or an emoji reaction.",
        "parameters": {
            "type": "object",
            "properties": {
                "query_or_url": {
                    "type": "string",
                    "description": "A direct HTTP(S) GIF/video URL or a concise search query such as 'happy dance' or 'confused'."
                }
            },
            "required": ["query_or_url"]
        }
    }
})

# Apply one explicit routing boundary to every terminal schema, including
# specialized session/job helpers whose individual descriptions focus on their
# mechanics rather than when they are appropriate.
_TERMINAL_TOOL_NAMES = {
    "run_command",
    "terminal_exec",
    "terminal_session_start",
    "terminal_session_write",
    "terminal_session_read",
    "terminal_session_close",
    "terminal_job_start",
    "terminal_job_status",
    "terminal_job_read",
    "terminal_job_wait",
    "terminal_job_cancel",
    "terminal_list_sessions",
    "terminal_list_jobs",
}
_TERMINAL_ROUTING = (
    "TERMINAL ROUTING: Use this tool only when the user is asking for programming "
    "or development work, inspecting or editing local files, or a task that "
    "genuinely requires shell/OS access. Do not use terminal tools for general "
    "questions, ordinary conversation, web research, calculations, image "
    "generation, document extraction, or work handled by a dedicated tool. "
)
for _schema in tools_schema:
    _function = _schema.get("function") if isinstance(_schema, dict) else None
    if isinstance(_function, dict) and _function.get("name") in _TERMINAL_TOOL_NAMES:
        _function["description"] = _TERMINAL_ROUTING + str(_function.get("description") or "")

# Canonical discovery metadata is layered over (and never replaces) the full
# internal registry above.  Importing this at the end avoids a circular import
# and keeps legacy consumers of AVAILABLE_TOOLS/tools_schema unchanged.
from emery.tool_search_catalog import build_tool_catalog

TOOL_CATALOG = build_tool_catalog(AVAILABLE_TOOLS, tools_schema)
TOOL_METADATA = TOOL_CATALOG
