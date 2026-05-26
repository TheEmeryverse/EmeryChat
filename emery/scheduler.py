import os
import json
import uuid
import logging
import re
from datetime import datetime, time, timedelta
from collections import deque

from emery.config import USER_TIMEZONE, JOBS_FILE_PATH
import emery.globals as globals

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6
}


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
    if not os.path.exists(JOBS_FILE_PATH):
        return []
    try:
        with open(JOBS_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
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
        logging.info(f"📅 SCHEDULER: Removed job {job_id} from persistent store.")

async def send_safe_job_message(bot, chat_id: int, text: str):
    """Splits large messages to fit Telegram's character limits."""
    MAX_LIMIT = 4000
    if len(text) <= MAX_LIMIT:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return

    while len(text) > 0:
        if len(text) <= MAX_LIMIT:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            break
            
        split_index = text.rfind('\n', 0, MAX_LIMIT)
        if split_index == -1 or split_index < 3000:
            split_index = MAX_LIMIT
            
        chunk = text[:split_index]
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
        text = text[split_index:].strip()

async def run_custom_job(context):
    """Callback triggered by APScheduler/JobQueue when a job fires."""
    job = context.job
    job_data = job.data
    job_id = job_data.get("id")
    prompt = job_data.get("prompt")
    description = job_data.get("description")
    chat_id = job_data.get("chat_id")
    schedule_type = job_data.get("schedule_type")
    
    if not chat_id:
        chat_id = globals.TARGET_CHAT_ID
        
    if not chat_id:
        logging.warning(f"⚠️ Custom job '{description}' ({job_id}) triggered without chat_id.")
        return
        
    if schedule_type == "yearly":
        try:
            # schedule_value format: "MM-DD HH:MM", e.g., "12-19 08:30"
            parts = job_data.get("schedule_value", "").split()
            if parts:
                target_month = int(parts[0].split("-")[0])
                now = datetime.now(USER_TIMEZONE)
                if now.month != target_month:
                    logging.info(f"📅 SCHEDULER: Skipping yearly job '{description}' ({job_id}) because the current month ({now.month}) does not match target month ({target_month}).")
                    return
        except Exception as e:
            logging.error(f"❌ SCHEDULER: Error filtering month for yearly job {job_id}: {e}", exc_info=True)
        
    logging.info(f"📅 CUSTOM JOB: '{description}' ({job_id}) starting...")
    from emery.engine import emery_engine
    from emery.helpers import emery_format
    
    try:
        # Run the engine with the job prompt
        res_text, _ = await emery_engine(deque([{"role": "user", "content": prompt}]))
        # Send formatted reply
        await send_safe_job_message(
            context.bot,
            chat_id=chat_id,
            text=f"🛡️ <b>EMERYCHAT JOB: {description}</b>\n\n{emery_format(res_text)}"
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
            logging.info(f"📅 SCHEDULER: Removed job '{job_id}' from Telegram JobQueue.")

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
            logging.info(f"📅 SCHEDULER: Scheduled daily job '{job_id}' at {sval}")
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
            logging.info(f"📅 SCHEDULER: Scheduled repeating job '{job_id}' every {seconds}s")
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
            logging.info(f"📅 SCHEDULER: Scheduled one-off job '{job_id}' at {dt_localized}")
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
            logging.info(f"📅 SCHEDULER: Scheduled weekly job '{job_id}' on {day_name}s at {time_str}")
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
            logging.info(f"📅 SCHEDULER: Scheduled monthly job '{job_id}' on day {day_of_month} at {time_str}")
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
            logging.info(f"📅 SCHEDULER: Scheduled yearly job '{job_id}' on {month}-{day} at {time_str} (via monthly trigger)")
            return True
            
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Failed to schedule job {job_id} in Telegram queue: {e}", exc_info=True)
        return False
        
    return False

# --- LLM TOOL CALLABLE FUNCTIONS ---

async def add_scheduled_job(schedule_type: str, schedule_value: str, prompt: str, description: str = None) -> str:
    """
    Schedule a new automated job/task.
    
    Parameters:
    - schedule_type: 'daily' (e.g. at 08:30), 'interval' (repeating, e.g. every '1h'), or 'once' (one-time date or offset).
    - schedule_value: The scheduling specification. E.g. '08:30' for daily, '30m' or '3600' for interval, or '2026-05-26 15:30:00' / '15m' for once.
    - prompt: The text prompt the bot executes when the job runs.
    - description: A short, user-friendly label/description of the job.
    """
    chat_id = globals.TARGET_CHAT_ID
    if not chat_id:
        return "Error: No active chat session to associate with this job. Run this command from within a chat."
        
    if not description:
        description = f"Reminder: {prompt[:30]}..." if len(prompt) > 30 else f"Reminder: {prompt}"

        
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
        
    job_data = {
        "id": job_id,
        "schedule_type": stype,
        "schedule_value": schedule_value,
        "prompt": prompt,
        "description": description,
        "chat_id": chat_id,
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
    logging.info("📅 SCHEDULER: Loading and registering persistent jobs...")
    jobs = load_jobs_from_file()
    count = 0
    for job_data in jobs:
        if schedule_in_tg_queue(job_data):
            count += 1
    logging.info(f"📅 SCHEDULER: Loaded and scheduled {count}/{len(jobs)} jobs from file.")

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
            logging.info(f"📅 SCHEDULER: Associating job '{job_data['description']}' ({job_data['id']}) with chat_id {chat_id}")
            # Reschedule it so it uses the updated chat_id in memory
            schedule_in_tg_queue(job_data)
            updated = True
            
    if updated:
        save_jobs_to_file(jobs)

