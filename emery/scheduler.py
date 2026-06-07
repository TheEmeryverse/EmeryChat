import os
import json
import uuid
import logging
import re
from datetime import datetime, time, timedelta
from collections import deque
from pathlib import Path

from telegram.error import BadRequest

from emery.config import (
    USER_TIMEZONE, JOBS_FILE_PATH, TELEGRAM_GROUP_CHAT_ID,
    ROUTINES_TOPIC_ID, CHAT_TOPIC_ID
)
import emery.globals as globals
from emery.telegram_utils import normalize_message_thread_id

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6
}

LEGACY_JOBS_FILE_PATH = str(Path(JOBS_FILE_PATH).resolve().parent.parent / "custom_jobs.json")


def parse_duration_to_seconds(val: str) -> int:
    """
    Parses duration strings like '1h', '30m', '10s', '1d', or a raw integer string
    into seconds.
    """
    val = val.strip().lower()
    if val.isdigit():
        return int(val)
    match = re.match(r'^(\d+)([hmsd])$', val)
    if match:
        num, unit = match.groups()
        num = int(num)
        if unit == 's':
            return num
        elif unit == 'm':
            return num * 60
        elif unit == 'h':
            return num * 3600
        elif unit == 'd':
            return num * 86400
    raise ValueError(f"Invalid duration format: '{val}'. Use e.g. '1h', '30m', '10s', '1d', or a number of seconds.")

def load_jobs_from_file() -> list:
    """Loads scheduled jobs from the custom_jobs.json file."""
    current_path = Path(JOBS_FILE_PATH)
    legacy_path = Path(LEGACY_JOBS_FILE_PATH)

    if not current_path.exists() and not legacy_path.exists():
        return []

    try:
        current_jobs = []
        if current_path.exists():
            with current_path.open("r", encoding="utf-8") as f:
                current_jobs = json.load(f)
        if not isinstance(current_jobs, list):
            current_jobs = []

        legacy_jobs = []
        if legacy_path.exists():
            with legacy_path.open("r", encoding="utf-8") as f:
                legacy_jobs = json.load(f)
        if not isinstance(legacy_jobs, list):
            legacy_jobs = []

        if (not current_jobs) and legacy_jobs:
            save_jobs_to_file(legacy_jobs)
            logging.info(
                "📅 SCHEDULER: Migrated %s legacy jobs from %s to %s",
                len(legacy_jobs),
                legacy_path,
                current_path,
            )
            current_jobs = legacy_jobs

        deduped_jobs = []
        seen_ids = set()
        for job in reversed(current_jobs):
            job_id = str(job.get("id") or "").strip()
            if not job_id:
                logging.warning("⚠️ SCHEDULER: Skipping persisted job without an id: %r", job)
                continue
            if job_id in seen_ids:
                logging.warning("⚠️ SCHEDULER: Dropping duplicate persisted job id '%s'", job_id)
                continue
            job["message_thread_id"] = normalize_message_thread_id(
                job.get("chat_id"),
                job.get("message_thread_id"),
            )
            seen_ids.add(job_id)
            deduped_jobs.append(job)
        deduped_jobs.reverse()

        if deduped_jobs != current_jobs:
            save_jobs_to_file(deduped_jobs)

        return deduped_jobs
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Error loading jobs file: {e}", exc_info=True)
        return []

def save_jobs_to_file(jobs: list):
    """Saves scheduled jobs to the custom_jobs.json file."""
    try:
        with open(JOBS_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Error saving jobs file: {e}", exc_info=True)

def remove_job_from_store(job_id: str):
    """Removes a job definition from the persistent JSON storage."""
    jobs = load_jobs_from_file()
    updated_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(jobs) != len(updated_jobs):
        save_jobs_to_file(updated_jobs)
        logging.info(f"📅 SCHEDULER: Removed job {job_id} from store")

async def send_safe_job_message(bot, chat_id: int, text: str, message_thread_id: int = None):
    """Splits large messages to fit Telegram's character limits."""
    MAX_LIMIT = 4000
    message_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
    try:
        if len(text) <= MAX_LIMIT:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", message_thread_id=message_thread_id)
            return True

        while len(text) > 0:
            if len(text) <= MAX_LIMIT:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", message_thread_id=message_thread_id)
                break

            split_index = text.rfind('\n', 0, MAX_LIMIT)
            if split_index == -1 or split_index < 3000:
                split_index = MAX_LIMIT

            chunk = text[:split_index]
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML", message_thread_id=message_thread_id)
            text = text[split_index:].strip()
        return True
    except BadRequest as e:
        logging.warning(
            "⚠️ SCHEDULER: Telegram rejected job message for chat_id=%s thread_id=%s: %s",
            chat_id,
            message_thread_id,
            e,
        )
        return False
    except Exception as e:
        logging.error(
            "❌ SCHEDULER: Unexpected error while sending job message to chat_id=%s thread_id=%s: %s",
            chat_id,
            message_thread_id,
            e,
            exc_info=True,
        )
        return False

async def run_custom_job(context):
    """Callback triggered by APScheduler/JobQueue when a job fires."""
    job = context.job
    job_data = job.data
    job_id = job_data.get("id")
    
    # Restore user context for this task
    user_id = job_data.get("user_id")
    target_user_id = job_data.get("target_user_id")
    target_user_name = job_data.get("target_user_name")
    
    if target_user_id is not None and target_user_id != -1:
        globals.current_user_id.set(target_user_id)
    elif user_id is not None:
        globals.current_user_id.set(user_id)
        
    prompt = job_data.get("prompt")
    description = job_data.get("description")
    chat_id = job_data.get("chat_id")
    message_thread_id = job_data.get("message_thread_id")
    schedule_type = job_data.get("schedule_type")
    route_to_routines = job_data.get("route_to_routines", True)
    
    # Check if it is an automated daily or repeating routine
    is_routine = schedule_type in ("daily", "weekly", "monthly", "yearly", "interval")
    
    if is_routine and route_to_routines:
        # Automated routine jobs must go to the designated group and topic
        if TELEGRAM_GROUP_CHAT_ID is not None:
            chat_id = TELEGRAM_GROUP_CHAT_ID
        else:
            logging.warning(
                "⚠️ SCHEDULER: Job '%s' (%s) is marked route_to_routines=True, "
                "but telegram.group_chat_id is not configured. Falling back to stored chat_id=%s.",
                description,
                job_id,
                chat_id,
            )
        if ROUTINES_TOPIC_ID is not None:
            message_thread_id = ROUTINES_TOPIC_ID
        else:
            logging.warning(
                "⚠️ SCHEDULER: Job '%s' (%s) is marked route_to_routines=True, "
                "but telegram.routines_topic_id is not configured. Using thread_id=%s.",
                description,
                job_id,
                message_thread_id,
            )
    else:
        # One-off reminders: fallback to CHAT_TOPIC_ID if they have no thread ID
        if message_thread_id is None and CHAT_TOPIC_ID is not None:
            message_thread_id = CHAT_TOPIC_ID

    if not chat_id:
        chat_id = globals.TARGET_CHAT_ID.get()
        
    if not chat_id:
        logging.warning(f"⚠️ Custom job '{description}' ({job_id}) triggered without chat_id.")
        return

    # Set context variables for target chat and thread/topic
    globals.TARGET_CHAT_ID.set(chat_id)
    message_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
    globals.CURRENT_THREAD_ID.set(message_thread_id)

    logging.info(
        "📅 SCHEDULER: Resolved destination for job '%s' (%s) -> chat_id=%s thread_id=%s "
        "[schedule_type=%s route_to_routines=%s]",
        description,
        job_id,
        chat_id,
        message_thread_id,
        schedule_type,
        route_to_routines,
    )
        
    if schedule_type == "yearly":
        try:
            # schedule_value format: "MM-DD HH:MM", e.g., "12-19 08:30"
            parts = job_data.get("schedule_value", "").split()
            if parts:
                target_month = int(parts[0].split("-")[0])
                now = datetime.now(USER_TIMEZONE)
                if now.month != target_month:
                    logging.info(f"📅 SCHEDULER: Skipping yearly job '{job_id}' (month mismatch: {now.month} != {target_month})")
                    return
        except Exception as e:
            logging.error(f"❌ SCHEDULER: Error filtering month for yearly job {job_id}: {e}", exc_info=True)
        
    logging.info(f"📅 SCHEDULER: Running custom job '{description}' ({job_id})")
    from emery.engine import emery_engine
    from emery.helpers import emery_format, telegram_escape
    
    try:
        active_user_id = globals.current_user_id.get() or user_id
        from emery.config import get_user_profile
        profile = get_user_profile(active_user_id)
        user_name = profile["name"]
        
        mention_prefix = ""
        if target_user_id == -1:
            from emery.config import PRIMARY_USER_ID, SECONDARY_USER_ID, USER_NAME, USER_2_NAME
            if SECONDARY_USER_ID != 0:
                mention_prefix = f"<a href=\"tg://user?id={PRIMARY_USER_ID}\">{USER_NAME}</a> & <a href=\"tg://user?id={SECONDARY_USER_ID}\">{USER_2_NAME}</a>: "
            else:
                mention_prefix = f"<a href=\"tg://user?id={PRIMARY_USER_ID}\">{USER_NAME}</a>: "
        elif target_user_id and target_user_id != 0:
            mention_prefix = f"<a href=\"tg://user?id={target_user_id}\">{target_user_name}</a>: "
            
        exec_prompt = prompt
        if schedule_type == "once" or "remind" in description.lower() or "remind" in prompt.lower():
            if target_user_id == -1:
                exec_prompt = (
                    f"{prompt}\n\n"
                    f"[SYSTEM DIRECTIVE: Deliver this reminder directly and concisely. "
                    f"Address both users (e.g. '{USER_NAME} and {USER_2_NAME}, time to head out'). "
                    f"Do not write conversational filler or say 'I have set a reminder'.]"
                )
            else:
                exec_prompt = (
                    f"{prompt}\n\n"
                    f"[SYSTEM DIRECTIVE: Deliver this reminder directly and concisely to the user, "
                    f"addressing them by name (e.g. '{user_name}, check the chicken's temperature.'). "
                    f"Do not write conversational filler or say 'I have set a reminder'.]"
                )
            
        # Run the engine with the job prompt
        res_text, _ = await emery_engine(deque([{"role": "user", "content": exec_prompt}]))
        # Send formatted reply
        sent_ok = await send_safe_job_message(
            context.bot,
            chat_id=chat_id,
            text=f"🛡️ <b>EMERYCHAT JOB: {telegram_escape(description)}</b>\n\n{mention_prefix}{emery_format(res_text)}",
            message_thread_id=message_thread_id
        )
        if not sent_ok:
            logging.warning(
                "⚠️ SCHEDULER: Job '%s' (%s) executed, but delivery to chat_id=%s thread_id=%s failed.",
                description,
                job_id,
                chat_id,
                message_thread_id,
            )
    except Exception as e:
        logging.error(f"❌ CUSTOM JOB Error executing job {job_id}: {e}", exc_info=True)
        
    # If it is a one-off run, clean it up from persistent store
    if schedule_type == "once":
        remove_job_from_store(job_id)

def remove_job_from_queue(job_id: str):
    """Removes a job from the active Telegram JobQueue."""
    if globals.application and globals.application.job_queue:
        current_jobs = globals.application.job_queue.get_jobs_by_name(job_id)
        if current_jobs:
            for j in current_jobs:
                j.schedule_removal()
            logging.info(f"📅 SCHEDULER: Cancelled job '{job_id}' in queue")

def schedule_in_tg_queue(job_data: dict) -> bool:
    """Registers the job in the active Telegram JobQueue based on its configuration."""
    if not globals.application or not globals.application.job_queue:
        logging.warning("⚠️ SCHEDULER: Telegram JobQueue is not initialized yet.")
        return False
        
    job_id = job_data["id"]
    stype = job_data["schedule_type"]
    sval = job_data["schedule_value"]
    
    # Clean up existing instance of job first
    remove_job_from_queue(job_id)
    
    try:
        if stype == "daily":
            # HH:MM format
            hour, minute = map(int, sval.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_daily(
                run_custom_job,
                time=time_obj,
                data=job_data,
                name=job_id
            )
            logging.info(f"📅 SCHEDULER: Scheduled daily '{job_id}' at {sval}")
            return True
            
        elif stype == "interval":
            seconds = parse_duration_to_seconds(sval)
            globals.application.job_queue.run_repeating(
                run_custom_job,
                interval=seconds,
                first=seconds,
                data=job_data,
                name=job_id
            )
            logging.info(f"📅 SCHEDULER: Scheduled repeating '{job_id}' every {seconds}s")
            return True
            
        elif stype == "once":
            if "-" in sval or ":" in sval:
                dt_naive = datetime.strptime(sval, "%Y-%m-%d %H:%M:%S")
                if hasattr(USER_TIMEZONE, "localize"):
                    dt_localized = USER_TIMEZONE.localize(dt_naive)
                else:
                    dt_localized = dt_naive.replace(tzinfo=USER_TIMEZONE)
            else:
                seconds = parse_duration_to_seconds(sval)
                dt_localized = datetime.now(USER_TIMEZONE) + timedelta(seconds=seconds)
                
            if dt_localized < datetime.now(USER_TIMEZONE):
                logging.warning(f"⚠️ SCHEDULER: Job '{job_id}' scheduled in the past ({dt_localized}), skipping.")
                remove_job_from_store(job_id)
                return False
                
            globals.application.job_queue.run_once(
                run_custom_job,
                when=dt_localized,
                data=job_data,
                name=job_id
            )
            logging.info(f"📅 SCHEDULER: Scheduled one-off '{job_id}' at {dt_localized.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
            
        elif stype == "weekly":
            # format: "Monday 08:30"
            parts = sval.split()
            day_name, time_str = parts
            weekday_index = WEEKDAYS[day_name.lower()]
            hour, minute = map(int, time_str.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_daily(
                run_custom_job,
                time=time_obj,
                days=(weekday_index,),
                data=job_data,
                name=job_id
            )
            logging.info(f"📅 SCHEDULER: Scheduled weekly '{job_id}' on {day_name}s at {time_str}")
            return True
            
        elif stype == "monthly":
            # format: "1 08:30"
            parts = sval.split()
            dom_str, time_str = parts
            day_of_month = int(dom_str)
            hour, minute = map(int, time_str.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_monthly(
                run_custom_job,
                time=time_obj,
                day=day_of_month,
                data=job_data,
                name=job_id
            )
            logging.info(f"📅 SCHEDULER: Scheduled monthly '{job_id}' on day {day_of_month} at {time_str}")
            return True
            
        elif stype == "yearly":
            # format: "12-19 08:30"
            parts = sval.split()
            date_str, time_str = parts
            month, day = map(int, date_str.split("-"))
            hour, minute = map(int, time_str.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_monthly(
                run_custom_job,
                time=time_obj,
                day=day,
                data=job_data,
                name=job_id
            )
            logging.info(f"📅 SCHEDULER: Scheduled yearly '{job_id}' on {month}-{day} at {time_str}")
            return True
            
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Failed to schedule job {job_id} in Telegram queue: {e}", exc_info=True)
        return False
        
    return False

# --- LLM TOOL CALLABLE FUNCTIONS ---

async def add_scheduled_job(schedule_type: str, schedule_value: str, prompt: str, description: str = None, target_user: str = None, route_to_routines: bool = None) -> str:
    """
    Schedule a new automated job/task.
    
    Parameters:
    - schedule_type: 'daily' (e.g. at 08:30), 'interval' (repeating, e.g. every '1h'), or 'once' (one-time date or offset).
    - schedule_value: The scheduling specification. E.g. '08:30' for daily, '30m' or '3600' for interval, or '2026-05-26 15:30:00' / '15m' for once.
    - prompt: The text prompt the bot executes when the job runs.
    - description: A short, user-friendly label/description of the job.
    - target_user: Optional name of the user this job/reminder is targeted at (e.g. 'Alice', 'Bob', or 'both').
    - route_to_routines: Optional boolean. If True, the routine is routed to the global routines topic. If False, it goes to the origin chat.
    """
    chat_id = globals.TARGET_CHAT_ID.get()
    if not chat_id:
        return "Error: No active chat session to associate with this job. Run this command from within a chat."
        
    if not description:
        description = f"Reminder: {prompt[:30]}..." if len(prompt) > 30 else f"Reminder: {prompt}"

    if route_to_routines is None:
        # Default to False for DMs (chat_id > 0), True for Groups (chat_id < 0)
        route_to_routines = False if chat_id > 0 else True

        
    stype = schedule_type.lower().strip()
    if stype not in ("daily", "interval", "once", "weekly", "monthly", "yearly"):
        return f"Error: Invalid schedule_type '{schedule_type}'. Must be 'daily', 'interval', 'once', 'weekly', 'monthly', or 'yearly'."
        
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    
    # Basic input validations before writing to store
    try:
        if stype == "daily":
            if ":" not in schedule_value:
                return "Error: For 'daily' job, schedule_value must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, schedule_value.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
        elif stype == "interval":
            parse_duration_to_seconds(schedule_value)
        elif stype == "once":
            if "-" in schedule_value or ":" in schedule_value:
                datetime.strptime(schedule_value, "%Y-%m-%d %H:%M:%S")
            else:
                parse_duration_to_seconds(schedule_value)
        elif stype == "weekly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return "Error: For 'weekly' job, schedule_value must be in '<day_name> <HH:MM>' format (e.g. 'Monday 08:30')."
            day_name, time_str = parts
            if day_name.lower() not in WEEKDAYS:
                return f"Error: Invalid weekday name '{day_name}'."
            if ":" not in time_str:
                return "Error: Time portion must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
        elif stype == "monthly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return "Error: For 'monthly' job, schedule_value must be in '<day_of_month> <HH:MM>' format (e.g. '1 12:00')."
            dom_str, time_str = parts
            if not dom_str.isdigit():
                return "Error: Day of month must be a number."
            dom = int(dom_str)
            if not (1 <= dom <= 31):
                return "Error: Day of month must be between 1 and 31."
            if ":" not in time_str:
                return "Error: Time portion must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
        elif stype == "yearly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return "Error: For 'yearly' job, schedule_value must be in '<MM-DD> <HH:MM>' format (e.g. '12-19 08:30')."
            date_str, time_str = parts
            if "-" not in date_str:
                return "Error: Date portion must be in 'MM-DD' format."
            month, day = map(int, date_str.split("-"))
            if not (1 <= month <= 12):
                return "Error: Month must be between 1 and 12."
            if not (1 <= day <= 31):
                return "Error: Day must be between 1 and 31."
            if ":" not in time_str:
                return "Error: Time portion must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
    except Exception as e:
        return f"Error validating schedule_value '{schedule_value}': {e}"
        
    creator_user_id = globals.current_user_id.get()
    
    # Resolve target user
    from emery.config import PRIMARY_USER_ID, SECONDARY_USER_ID, USER_NAME, USER_2_NAME, get_user_profile
    target_user_id = creator_user_id
    target_name = get_user_profile(creator_user_id)["name"]
    
    if target_user:
        clean_name = target_user.strip().lower()
        if clean_name in ("both", "us", "family", "everyone", "all"):
            target_user_id = -1
            target_name = "both"
        elif clean_name in (USER_NAME.lower(), "me", "myself"):
            target_user_id = PRIMARY_USER_ID
            target_name = USER_NAME
        elif SECONDARY_USER_ID != 0 and clean_name in (USER_2_NAME.lower(), "wife", "spouse", "her"):
            target_user_id = SECONDARY_USER_ID
            target_name = USER_2_NAME

    job_data = {
        "id": job_id,
        "schedule_type": stype,
        "schedule_value": schedule_value,
        "prompt": prompt,
        "description": description,
        "chat_id": chat_id,
        "message_thread_id": globals.CURRENT_THREAD_ID.get(),
        "route_to_routines": route_to_routines,
        "user_id": creator_user_id,
        "target_user_id": target_user_id,
        "target_user_name": target_name,
        "created_at": datetime.now(USER_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    }
    
    # Save definition to persistent store
    jobs = load_jobs_from_file()
    jobs.append(job_data)
    save_jobs_to_file(jobs)
    
    # Schedule inside Telegram's job queue
    success = schedule_in_tg_queue(job_data)
    if success:
        return f"Successfully scheduled job '{description}' (ID: {job_id}) to run with trigger '{stype}' and schedule '{schedule_value}'."
    else:
        # Rollback from store if scheduling failed
        remove_job_from_store(job_id)
        return "Error: Failed to register job in Telegram's active job queue."

async def list_scheduled_jobs() -> str:
    """Lists all active custom scheduled jobs."""
    jobs = load_jobs_from_file()
    if not jobs:
        return "No custom scheduled jobs are currently configured."
        
    lines = ["Here are the currently configured custom scheduled jobs:"]
    for j in jobs:
        lines.append(
            f"📌 **{j['description']}** (ID: `{j['id']}`)\n"
            f"   - **Trigger**: {j['schedule_type'].upper()} ({j['schedule_value']})\n"
            f"   - **Prompt**: \"{j['prompt']}\"\n"
            f"   - **Created At**: {j['created_at']}"
        )
    return "\n\n".join(lines)

async def remove_scheduled_job(job_id: str) -> str:
    """Cancels and removes a custom job by its unique ID."""
    job_id = job_id.strip()
    jobs = load_jobs_from_file()
    job_to_remove = next((j for j in jobs if j["id"] == job_id), None)
    
    if not job_to_remove:
        return f"Error: No scheduled job found with ID '{job_id}'."
        
    # Cancel in telegram queue
    remove_job_from_queue(job_id)
    
    # Remove from persistent JSON store
    remove_job_from_store(job_id)
    
    return f"Successfully cancelled and removed job '{job_to_remove['description']}' (ID: `{job_id}`)."

# --- STARTUP FUNCTION ---

def load_and_register_all_jobs():
    """Loads all saved custom jobs from file and schedules them in the queue on boot."""
    jobs = load_jobs_from_file()
    count = 0
    for job_data in jobs:
        if schedule_in_tg_queue(job_data):
            count += 1
    logging.info(f"📅 SCHEDULER: Loaded {count}/{len(jobs)} persistent jobs")

def update_jobs_with_chat_id(chat_id: int):
    """
    Checks if any persistent scheduled jobs have a null/missing chat_id,
    updates them with the provided chat_id, saves the file, and reschedules them.
    This dynamically registers placeholder default jobs once the first user message is received.
    """
    jobs = load_jobs_from_file()
    updated = False
    for job_data in jobs:
        if job_data.get("chat_id") is None:
            job_data["chat_id"] = chat_id
            logging.info(f"📅 SCHEDULER: Associated job '{job_data['id']}' with chat {chat_id}")
            # Reschedule it so it uses the updated chat_id in memory
            schedule_in_tg_queue(job_data)
            updated = True
            
    if updated:
        save_jobs_to_file(jobs)
