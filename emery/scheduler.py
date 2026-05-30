import os
import json
import uuid
import logging
import re
from datetime import datetime, time, timedelta
from collections import deque
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from emery.config import (
    USER_TIMEZONE, JOBS_FILE_PATH, ENABLE_HEARTBEAT,
    HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_SILENCE_THRESHOLD_SECONDS,
    HEARTBEAT_SLEEP_START, HEARTBEAT_SLEEP_END
)
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
    if not os.path.exists(JOBS_FILE_PATH):
        return []
    try:
        with open(JOBS_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Error loading jobs file: {e}", exc_info=True)
        return []

def save_jobs_to_file(jobs: list):
    try:
        with open(JOBS_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Error saving jobs file: {e}", exc_info=True)

def remove_job_from_store(job_id: str):
    jobs = load_jobs_from_file()
    updated_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(jobs) != len(updated_jobs):
        save_jobs_to_file(updated_jobs)
        logging.info(f"📅 SCHEDULER: Removed job {job_id} from store")

async def run_custom_job(job_data: dict):
    """Callback triggered by APScheduler when a scheduled job fires."""
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
        # Check environment for alert configurations fallback
        alert_chat_id = os.getenv("ALERT_CHAT_ID") or os.getenv("TELEGRAM_GROUP_CHAT_ID")
        if alert_chat_id:
            chat_id = alert_chat_id
    
    if not chat_id:
        chat_id = globals.TARGET_CHAT_ID.get()
        
    if not chat_id:
        # Fallback default chat id if none configured
        chat_id = "default_system_job"
        
    # Set context variables for target chat and thread/topic
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(message_thread_id)
    
    # Initialize request/job-scoped outgoing responses container
    globals.outgoing_responses.set([])
        
    logging.info(f"📅 SCHEDULER: Running custom job '{description}' ({job_id})")
    from emery.engine import emery_engine
    from emery.helpers import emery_format
    
    try:
        active_user_id = globals.current_user_id.get() or user_id
        from emery.config import get_user_profile
        profile = get_user_profile(active_user_id)
        user_name = profile["name"]
        
        mention_prefix = ""
        if target_user_id == -1:
            from emery.config import PRIMARY_USER_ID, SECONDARY_USER_ID, USER_NAME, USER_2_NAME
            if SECONDARY_USER_ID != 0:
                mention_prefix = f"{USER_NAME} & {USER_2_NAME}: "
            else:
                mention_prefix = f"{USER_NAME}: "
        elif target_user_id and target_user_id != 0:
            mention_prefix = f"{target_user_name}: "
            
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
        
        # Split reasoning/thinking tag out
        start_tag = "<" + "think" + ">"
        end_tag = "</" + "think" + ">"
        pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
        think_match = re.search(pattern, res_text, re.DOTALL | re.IGNORECASE)

        clean_response = res_text
        thinking_content = ""

        if think_match:
            thinking_content = think_match.group(1).strip()
            clean_response = re.sub(pattern, '', res_text, flags=re.DOTALL | re.IGNORECASE).strip()
            
        handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
        if handshake_check != "DONE" and clean_response:
            formatted_text = f"🛡️ <b>EMERYCHAT JOB: {description}</b>\n\n{mention_prefix}{emery_format(clean_response)}"
            globals.outgoing_responses.get().insert(0, {
                "type": "text",
                "content": formatted_text
            })
            
        # Dispatch outputs to webhook
        from emery.api_helpers import send_responses_to_webhook
        await send_responses_to_webhook(str(chat_id), globals.outgoing_responses.get(), thinking=thinking_content)
        
    except Exception as e:
        logging.error(f"❌ CUSTOM JOB Error executing job {job_id}: {e}", exc_info=True)
        
    # If it is a one-off run, clean it up from persistent store
    if schedule_type == "once":
        remove_job_from_store(job_id)

def remove_job_from_queue(job_id: str):
    """Removes a job from the active APScheduler queue."""
    if globals.scheduler:
        try:
            globals.scheduler.remove_job(job_id)
            logging.info(f"📅 SCHEDULER: Cancelled job '{job_id}' in queue")
        except Exception:
            pass

def schedule_in_tg_queue(job_data: dict) -> bool:
    """Registers the job in the active APScheduler queue based on its configuration."""
    if not globals.scheduler:
        logging.warning("⚠️ SCHEDULER: APScheduler is not initialized yet.")
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
            globals.scheduler.add_job(
                run_custom_job,
                trigger='cron',
                hour=hour,
                minute=minute,
                args=[job_data],
                id=job_id,
                replace_existing=True
            )
            logging.info(f"📅 SCHEDULER: Scheduled daily '{job_id}' at {sval}")
            return True
            
        elif stype == "interval":
            seconds = parse_duration_to_seconds(sval)
            globals.scheduler.add_job(
                run_custom_job,
                trigger='interval',
                seconds=seconds,
                args=[job_data],
                id=job_id,
                replace_existing=True
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
                
            globals.scheduler.add_job(
                run_custom_job,
                trigger='date',
                run_date=dt_localized,
                args=[job_data],
                id=job_id,
                replace_existing=True
            )
            logging.info(f"📅 SCHEDULER: Scheduled one-off '{job_id}' at {dt_localized.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
            
        elif stype == "weekly":
            # format: "Monday 08:30"
            parts = sval.split()
            day_name, time_str = parts
            weekday_index = WEEKDAYS[day_name.lower()]
            hour, minute = map(int, time_str.split(":"))
            globals.scheduler.add_job(
                run_custom_job,
                trigger='cron',
                day_of_week=weekday_index,
                hour=hour,
                minute=minute,
                args=[job_data],
                id=job_id,
                replace_existing=True
            )
            logging.info(f"📅 SCHEDULER: Scheduled weekly '{job_id}' on {day_name}s at {time_str}")
            return True
            
        elif stype == "monthly":
            # format: "1 08:30"
            parts = sval.split()
            dom_str, time_str = parts
            day_of_month = int(dom_str)
            hour, minute = map(int, time_str.split(":"))
            globals.scheduler.add_job(
                run_custom_job,
                trigger='cron',
                day=day_of_month,
                hour=hour,
                minute=minute,
                args=[job_data],
                id=job_id,
                replace_existing=True
            )
            logging.info(f"📅 SCHEDULER: Scheduled monthly '{job_id}' on day {day_of_month} at {time_str}")
            return True
            
        elif stype == "yearly":
            # format: "12-19 08:30"
            parts = sval.split()
            date_str, time_str = parts
            month, day = map(int, date_str.split("-"))
            hour, minute = map(int, time_str.split(":"))
            globals.scheduler.add_job(
                run_custom_job,
                trigger='cron',
                month=month,
                day=day,
                hour=hour,
                minute=minute,
                args=[job_data],
                id=job_id,
                replace_existing=True
            )
            logging.info(f"📅 SCHEDULER: Scheduled yearly '{job_id}' on {month}-{day} at {time_str}")
            return True
            
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Failed to schedule job {job_id} in scheduler: {e}", exc_info=True)
        return False
        
    return False

# --- LLM TOOL CALLABLE FUNCTIONS ---

async def add_scheduled_job(schedule_type: str, schedule_value: str, prompt: str, description: str = None, target_user: str = None, route_to_routines: bool = None) -> str:
    chat_id = globals.TARGET_CHAT_ID.get()
    if not chat_id:
        return "Error: No active chat session to associate with this job. Run this command from within a chat."
        
    if not description:
        description = f"Reminder: {prompt[:30]}..." if len(prompt) > 30 else f"Reminder: {prompt}"

    if route_to_routines is None:
        # Default to False for DMs, True for Groups
        route_to_routines = False if isinstance(chat_id, int) and chat_id > 0 else True
        
    stype = schedule_type.lower().strip()
    if stype not in ("daily", "interval", "once", "weekly", "monthly", "yearly"):
        return f"Error: Invalid schedule_type '{schedule_type}'. Must be 'daily', 'interval', 'once', 'weekly', 'monthly', or 'yearly'."
        
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    
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
    
    jobs = load_jobs_from_file()
    jobs.append(job_data)
    save_jobs_to_file(jobs)
    
    success = schedule_in_tg_queue(job_data)
    if success:
        return f"Successfully scheduled job '{description}' (ID: {job_id}) to run with trigger '{stype}' and schedule '{schedule_value}'."
    else:
        remove_job_from_store(job_id)
        return "Error: Failed to register job in scheduler queue."

async def list_scheduled_jobs() -> str:
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
    job_id = job_id.strip()
    jobs = load_jobs_from_file()
    job_to_remove = next((j for j in jobs if j["id"] == job_id), None)
    
    if not job_to_remove:
        return f"Error: No scheduled job found with ID '{job_id}'."
        
    remove_job_from_queue(job_id)
    remove_job_from_store(job_id)
    
    return f"Successfully cancelled and removed job '{job_to_remove['description']}' (ID: `{job_id}`)."

# --- HEARTBEAT & SLEEP CHECKINS ---

async def heartbeat_check():
    """Callback for APScheduler that runs periodically to check if the bot should spontaneously send a message."""
    if not ENABLE_HEARTBEAT:
        return
        
    logging.info("💓 HEARTBEAT: Checking activity...")
    
    group_chat_id = os.getenv("ALERT_CHAT_ID") or os.getenv("TELEGRAM_GROUP_CHAT_ID")
    if not group_chat_id:
        logging.info("💓 HEARTBEAT: No target chat ID set, skipping heartbeat activity check.")
        return
        
    history = globals.chat_histories.get(group_chat_id)
    if not history:
        return
        
    now = datetime.now(USER_TIMEZONE)
    
    try:
        from datetime import time
        start_h, start_m = map(int, HEARTBEAT_SLEEP_START.split(':'))
        end_h, end_m = map(int, HEARTBEAT_SLEEP_END.split(':'))
        start_time = time(start_h, start_m)
        end_time = time(end_h, end_m)
        curr_time = now.time()
        
        is_asleep = False
        if start_time <= end_time:
            if start_time <= curr_time <= end_time:
                is_asleep = True
        else:
            if curr_time >= start_time or curr_time <= end_time:
                is_asleep = True
                
        if is_asleep:
            logging.info(f"💓 HEARTBEAT: Suppressed check-in (inside sleep window: {HEARTBEAT_SLEEP_START}-{HEARTBEAT_SLEEP_END})")
            return
    except Exception as e:
        logging.error(f"❌ HEARTBEAT: Error checking sleep window range: {e}")
        
    last_msg = history[-1]
    last_time = last_msg.get("timestamp")
    
    if not last_time:
        last_msg["timestamp"] = now
        return
        
    elapsed = (now - last_time).total_seconds()
    if elapsed > HEARTBEAT_SILENCE_THRESHOLD_SECONDS:
        logging.info(f"💓 HEARTBEAT: Chat {group_chat_id} silent for {elapsed:.1f}s, evaluating check-in...")
        await handle_heartbeat_trigger(group_chat_id)

async def handle_heartbeat_trigger(chat_id: str):
    """Triggers the model to check in on a silent chat."""
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(None)
    globals.outgoing_responses.set([])
    
    trigger_content = (
        f"[System Trigger (Heartbeat)]: It has been several hours since the last message in this chat. "
        f"Review the conversation history. You should be extremely conservative about initiating contact. "
        f"Only send a message if there is an important outstanding question, a topic you promised to follow up on, "
        f"or a highly natural reason to check in. If the conversation has reached a natural pause, or if a human "
        f"would typically let it rest, you MUST reply with exactly 'DONE' to remain completely silent. Do not send conversational filler."
    )
    
    trigger_msg = {
        "role": "user",
        "content": trigger_content,
        "is_heartbeat_trigger": True,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    
    globals.chat_histories[chat_id].append(trigger_msg)
    
    try:
        from emery.engine import emery_engine
        response_text, _ = await emery_engine(globals.chat_histories[chat_id])
    except Exception as e:
        logging.error(f"Error executing heartbeat engine: {e}")
        response_text = "DONE"
        
    if trigger_msg in globals.chat_histories[chat_id]:
        globals.chat_histories[chat_id].remove(trigger_msg)
        
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    think_match = re.search(pattern, response_text, re.DOTALL | re.IGNORECASE)
    
    clean_response = response_text
    thinking_content = ""
    
    if think_match:
        thinking_content = think_match.group(1).strip()
        clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
        
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.info(f"🤫 HEARTBEAT: Chat {chat_id} remains silent.")
        if globals.chat_histories[chat_id]:
            globals.chat_histories[chat_id][-1]["timestamp"] = datetime.now(USER_TIMEZONE)
        return
        
    globals.chat_histories[chat_id].append({
        "role": "assistant",
        "content": response_text,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    from emery.helpers import emery_format
    globals.outgoing_responses.get().insert(0, {
        "type": "text",
        "content": emery_format(clean_response)
    })
    
    from emery.api_helpers import send_responses_to_webhook
    await send_responses_to_webhook(str(chat_id), globals.outgoing_responses.get(), thinking=thinking_content)

# --- STARTUP FUNCTIONS ---

def start_scheduler():
    """Initializes and starts the AsyncIOScheduler instance."""
    if globals.scheduler is None:
        globals.scheduler = AsyncIOScheduler(timezone=USER_TIMEZONE)
        globals.scheduler.start()
        logging.info("📅 SCHEDULER: APScheduler AsyncIOScheduler started.")
        
        # Register Heartbeat spontaneous check-in task
        if ENABLE_HEARTBEAT:
            globals.scheduler.add_job(
                heartbeat_check,
                trigger='interval',
                seconds=HEARTBEAT_INTERVAL_SECONDS,
                id="heartbeat_checker",
                replace_existing=True
            )
            logging.info(f"💓 HEARTBEAT: Spontaneous heartbeat active (checking every {HEARTBEAT_INTERVAL_SECONDS}s)")

def load_and_register_all_jobs():
    jobs = load_jobs_from_file()
    count = 0
    for job_data in jobs:
        if schedule_in_tg_queue(job_data):
            count += 1
    logging.info(f"📅 SCHEDULER: Loaded {count}/{len(jobs)} persistent jobs")

def update_jobs_with_chat_id(chat_id: str):
    jobs = load_jobs_from_file()
    updated = False
    for job_data in jobs:
        if job_data.get("chat_id") is None:
            job_data["chat_id"] = chat_id
            logging.info(f"📅 SCHEDULER: Associated job '{job_data['id']}' with chat {chat_id}")
            schedule_in_tg_queue(job_data)
            updated = True
            
    if updated:
        save_jobs_to_file(jobs)
