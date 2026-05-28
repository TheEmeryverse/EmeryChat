import os
import re
import logging
import asyncio

from emery.config import (
    MEMORY_FILE_PATH, MEMORY_THRESHOLD, USER_NAME, USER_LOCATION,
    USER_TIMEZONE, USER_BIRTHDAY, USER_FAMILY, USER_PROFESSION,
    CAMERA_LOG_FILE_PATH, get_user_profile, get_memory_file_path
)
import emery.globals as globals

def retrieve_relevant_memories(user_query: str, user_id: int = None) -> str:
    """
    Reads memory.md and performs keyword filtering against the user's latest query
    to load only relevant memories, keeping the CPU-only prompt evaluation window small.
    """
    if user_id is None:
        user_id = globals.current_user_id.get()
        
    memory_file_path = get_memory_file_path(user_id)
    if not memory_file_path or not os.path.exists(memory_file_path):
        return ""
        
    try:
        with open(memory_file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # If the file is small, load it entirely to ensure maximum context
        if len(content) < MEMORY_THRESHOLD:
            return content
            
        # If larger, parse and filter sections to save context tokens on CPU
        lines = content.splitlines()
        
        # Simple parser to separate critical header sections (Profile, Context)
        # from General Facts section which we will filter by keyword.
        profile_context_lines = []
        general_facts_lines = []
        
        current_section = None
        recent_topics_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                current_section = stripped.lower()
                
            # Keep header sections intact
            if current_section in ["## user profile & preferences", "## project & system context"]:
                profile_context_lines.append(line)
            # Route general facts and raw memory intake to general_facts_lines for filtering
            elif current_section in ["## general facts & logs", "## raw memory intake"]:
                # Keep section headers, but only filter bullets
                if stripped.startswith("## ") or not stripped:
                    general_facts_lines.append(line)
                elif stripped.startswith("- ") or stripped.startswith("* "):
                    general_facts_lines.append(line)
            # Route conversational topics log to its own list so we can pull the latest N
            elif current_section in ["## conversational topics log"]:
                if stripped.startswith("- ") or stripped.startswith("* "):
                    recent_topics_lines.append(line)
            else:
                # Outside major sections (like main title)
                if not stripped.startswith("## "):
                    profile_context_lines.append(line)
                    
        # Tokenize user query to extract keywords
        # 0. Strip leading timestamp prefix if present (e.g. "[Monday, May 26, 2026 at 04:43 PM]")
        query_text = re.sub(r'^\[[^\]]+\]\s*', '', user_query)
        
        # 1. Clean query (lowercase, remove punctuation)
        clean_query = re.sub(r'[^\w\s]', '', query_text.lower())
        words = clean_query.split()

        
        # 2. Exclude common stop words
        stop_words = {
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
            "please", "emery", "remember"
        }
        
        def stem_word(w: str) -> str:
            w = w.lower().strip()
            if w.endswith("'s"):
                w = w[:-2]
            elif w.endswith("s'"):
                w = w[:-2]
            elif len(w) > 4 and w.endswith("s"):
                if w.endswith("es"):
                    if w.endswith("ies") and len(w) > 5:
                        w = w[:-3] + "y"
                    else:
                        w = w[:-2]
                else:
                    w = w[:-1]
            return w

        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        stemmed_keywords = [stem_word(kw) for kw in keywords]
        
        if not stemmed_keywords:
            # If no significant keywords found, just return the profile sections to save space
            return "\n".join(profile_context_lines)
            
        # 3. Scan general facts and keep matching lines
        matched_facts = []
        for line in general_facts_lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                # It's a fact bullet, check for keyword match using stemming
                lower_fact = stripped.lower()
                fact_words = re.sub(r'[^\w\s]', '', lower_fact).split()
                stemmed_fact_words = [stem_word(fw) for fw in fact_words]
                
                # Check if any stemmed keyword matches a stemmed fact word
                match_found = False
                for kw in stemmed_keywords:
                    if any((kw in fw or fw in kw) for fw in stemmed_fact_words):
                        match_found = True
                        break
                
                if match_found:
                    matched_facts.append(line)
            else:
                # Keep structure/spacing
                matched_facts.append(line)
                
        # Extract the N most recent topic log entries
        N = 5
        recent_topics = recent_topics_lines[-N:]
        
        # Combine profile context with matched facts and recent topics
        final_memories = profile_context_lines
        if recent_topics:
            final_memories += ["\n## Recent Conversation Topics"] + recent_topics
            
        final_memories += ["\n## Relevant Recalled Memories"] + matched_facts
        return "\n".join(final_memories)
        
    except Exception as e:
        logging.error(f"❌ MEMORY ENGINE: Error retrieving memories: {e}", exc_info=True)
        return ""

async def save_user_memory(fact: str, user_id: int = None) -> str:
    """
    Saves a new fact, preference, or critical piece of information about the user or their environment
    to the persistent memory log. Use when the user shares something they expect you to remember long-term.
    """
    if user_id is None:
        user_id = globals.current_user_id.get()
        
    memory_file_path = get_memory_file_path(user_id)
    if not memory_file_path:
        return "Memory is disabled."
        
    logging.info(f"💾 MEMORY: Appending new fact to staging area for user {user_id}: '{fact}'")
    if not os.path.exists(memory_file_path):
        # Create default if missing
        profile = get_user_profile(user_id)
        baseline_template = (
            f"# Emery's Memory Log\n\n"
            f"## User Profile & Preferences\n"
            f"- Name: {profile['name']}\n"
            f"- Location: {USER_LOCATION}\n"
            f"- Timezone: {USER_TIMEZONE}\n"
            f"- Birthday: {profile['birthday']}\n"
            f"- Family: {profile['family']}\n"
            f"- Profession: {profile['profession']}\n\n"
            f"## General Facts & Logs\n\n"
            f"## Conversational Topics Log\n\n"
            f"## Raw Memory Intake\n"
        )
        with open(memory_file_path, "w", encoding="utf-8") as f:
            f.write(baseline_template)

    try:
        # Append to the Raw Memory Intake section of memory.md
        with open(memory_file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # Standardize Raw Memory Intake heading presence
        heading = "## Raw Memory Intake"
        if heading not in content:
            content += f"\n\n{heading}\n"
            
        # Insert the fact under the heading
        new_fact_line = f"- {fact.strip()}"
        
        # We find the heading and inject right after it
        parts = content.split(heading)
        prefix = parts[0].rstrip()
        suffix = parts[1].strip()
        
        updated_suffix = f"\n{new_fact_line}"
        if suffix:
            updated_suffix += f"\n{suffix}"
            
        updated_content = f"{prefix}\n\n{heading}{updated_suffix}\n"
        
        with open(memory_file_path, "w", encoding="utf-8") as f:
            f.write(updated_content)
            
        # Trigger background consolidation using the fast model
        logging.info(f"💾 MEMORY: Scheduling background memory consolidation for user {user_id}...")
        asyncio.create_task(consolidate_memory_background(user_id))
        
        return f"Successfully saved to memory log staging: '{fact}'"
        
    except Exception as e:
        logging.error(f"❌ MEMORY TOOL: Failed to write memory: {e}", exc_info=True)
        return f"Failed to save fact to memory: {e}"

_is_consolidating = set()
 
async def consolidate_memory_background(user_id: int = None) -> None:
    """
    A background consolidation task that reads memory.md, runs the coprocessor model
    to deduplicate, sort, and organize the list, and then saves it.
    This keeps the main chat model from blocking on heavy processing task execution.
    """
    global _is_consolidating
    if user_id is None:
        user_id = globals.current_user_id.get()
        
    if user_id in _is_consolidating:
        logging.info(f"💾 CONSOLIDATOR: Memory consolidation is already in progress for user {user_id}. Skipping duplicate run.")
        return
 
    logging.info(f"💾 CONSOLIDATOR: Starting background memory consolidation for user {user_id}...")
    
    memory_file_path = get_memory_file_path(user_id)
    if not memory_file_path or not os.path.exists(memory_file_path):
        logging.warning(f"⚠️ CONSOLIDATOR: Memory file '{memory_file_path}' does not exist. Aborting consolidation.")
        return
        
    _is_consolidating.add(user_id)
    try:
        # Prevent concurrent file reads/writes using a simple sleep
        await asyncio.sleep(0.5)
        
        with open(memory_file_path, "r", encoding="utf-8") as f:
            current_markdown = f.read()
            
        system_prompt = (
            "You are Emery's Memory Consolidation System. Your job is to process the memory log (written in Markdown) "
            "and merge any new facts or topic logs listed under '## Raw Memory Intake' into the main categories:\n"
            "- '## User Profile & Preferences'\n"
            "- '## General Facts & Logs'\n"
            "- '## Conversational Topics Log'\n\n"
            "Rules:\n"
            "1. Categorize all raw facts and topic logs from '## Raw Memory Intake' into their appropriate section. User preferences/profile details go to 'User Profile & Preferences', general facts go to 'General Facts & Logs', and topic summaries (bullets that start with dates/days and include [Tags: ...] at the end) go to 'Conversational Topics Log'.\n"
            "2. Completely empty/clear the '## Raw Memory Intake' section so it has no bullet points listed under it anymore.\n"
            "3. Deduplicate facts and topic entries. If a new topic summary covers the same discussion as an existing one, merge them or keep the more detailed one.\n"
            "4. Compact topics log: If there are multiple entries for the same day or week, merge them into a single concise bullet point describing all topics covered (e.g. '- On [Date]: Discussed OpenAI IPO, Artemis program, and weather. [Tags: space, rocket, ai, tech]').\n"
            "5. Resolve contradictions: if a new fact directly contradicts an old one, update it with the newer information and remove the obsolete one.\n"
            "6. Keep the exact markdown section structure. Maintain bullet points. Output ONLY a single, consolidated markdown document, starting with '# Emery's Memory Log'. Do not duplicate the document, repeat the headers, or output 'before' and 'after' versions. Do not include conversational remarks, explanations, or code block formatting like ```markdown."
        )
        
        user_prompt = f"Here is the current memory file content:\n\n{current_markdown}\n\nPlease consolidate it now."
        
        # Local import to break circular dependency
        from emery.helpers import query_fast_model
        consolidated = await query_fast_model(user_prompt, system_prompt)
        consolidated = consolidated.strip()
        
        if not consolidated or not consolidated.startswith("# Emery's Memory Log"):
            logging.error(f"❌ CONSOLIDATOR: Fast model returned invalid markdown. Aborting overwrite. Response: '{consolidated[:200]}...'")
            return
            
        # Safety Check: if the model duplicated the document, keep only the first document block
        if consolidated.count("# Emery's Memory Log") > 1:
            logging.warning("⚠️ CONSOLIDATOR: Model output contained multiple document blocks. Keeping only the first one.")
            consolidated = "# Emery's Memory Log" + consolidated.split("# Emery's Memory Log")[1]
            
        # Safety check: make sure the Conversational Topics Log section exists
        if "## Conversational Topics Log" not in consolidated:
            if "## Raw Memory Intake" in consolidated:
                consolidated = consolidated.replace("## Raw Memory Intake", "## Conversational Topics Log\n\n## Raw Memory Intake")
            else:
                consolidated = consolidated.replace("## Raw Memory Intake", "## Conversational Topics Log\n\n## Raw Memory Intake")
                
        # Safety check: make sure the Raw Memory Intake section exists
        if "## Raw Memory Intake" not in consolidated:
            consolidated += "\n\n## Raw Memory Intake\n"
            
        with open(memory_file_path, "w", encoding="utf-8") as f:
            f.write(consolidated)
            
        logging.info(f"💾 CONSOLIDATOR: Background memory consolidation for user {user_id} completed successfully!")
        
    except Exception as e:
        logging.error(f"❌ CONSOLIDATOR: Background task crash for user {user_id}: {e}", exc_info=True)
    finally:
        _is_consolidating.discard(user_id)
 
def wipe_memory(user_id: int = None) -> bool:
    """
    Overwrites memory.md with the default baseline template structure,
    clearing all custom saved facts and preferences.
    """
    if user_id is None:
        user_id = globals.current_user_id.get()
        
    memory_file_path = get_memory_file_path(user_id)
    if not memory_file_path:
        return False
        
    logging.info(f"🧠 MEMORY: Wiping all memories for user {user_id} and restoring baseline template...")
    profile = get_user_profile(user_id)
    baseline_template = (
        f"# Emery's Memory Log\n\n"
        f"## User Profile & Preferences\n"
        f"- Name: {profile['name']}\n"
        f"- Location: {USER_LOCATION}\n"
        f"- Timezone: {USER_TIMEZONE}\n"
        f"- Birthday: {profile['birthday']}\n"
        f"- Family: {profile['family']}\n"
        f"- Profession: {profile['profession']}\n\n"
        f"## General Facts & Logs\n\n"
        f"## Conversational Topics Log\n\n"
        f"## Raw Memory Intake\n"
    )
    try:
        with open(memory_file_path, "w", encoding="utf-8") as f:
            f.write(baseline_template)
        return True
    except Exception as e:
        logging.error(f"❌ WIPE MEMORY: Failed to wipe memory file: {e}", exc_info=True)
        return False

async def append_camera_log(camera_name: str, threat_report: str, scene_context: str) -> None:
    """
    Appends a new security camera event to camera_log.md and prunes entries older than 7 days.
    """
    from datetime import datetime, timedelta
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
            logging.error(f"❌ CAMERA LOG: Error reading camera log: {e}", exc_info=True)
            
    # Filter/prune old lines
    cutoff_date = (now_dt - timedelta(days=7)).date()
    pruned_lines = []
    
    for line in existing_lines:
        if line.strip() == "# Emery Camera Security Log":
            continue
        
        # Check if line is a log entry
        match = re.match(r'^-\s+\[(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}(?:\s+\w+)?\]', line)
        if match:
            date_str = match.group(1)
            try:
                entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if entry_date < cutoff_date:
                    continue  # prune
            except Exception:
                pass  # Keep if parsing fails
        
        if line.strip():
            pruned_lines.append(line)
            
    # Reassemble with the header at the top
    out_lines = [header, "\n"] + [line for line in pruned_lines if line.strip()]
    if not out_lines[-1].endswith("\n"):
        out_lines[-1] = out_lines[-1] + "\n"
    out_lines.append(new_entry)
    
    try:
        with open(CAMERA_LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.writelines(out_lines)
        logging.info(f"📹 CAMERA LOG: Logged activity for {camera_name} to camera_log.md")
    except Exception as e:
        logging.error(f"❌ CAMERA LOG: Failed to write camera log: {e}", exc_info=True)

async def get_camera_security_log(camera_name: str = None, limit: int = 10) -> str:
    """
    Retrieve recent security camera activity logs including AI threat reports and scene descriptions.
    Use when the user asks what happened on a camera, what activity was detected, or wants a security summary.
    """
    if not os.path.exists(CAMERA_LOG_FILE_PATH):
        return "No security camera logs are available."
        
    try:
        with open(CAMERA_LOG_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        entries = []
        for line in lines:
            if not line.strip() or line.startswith("#"):
                continue
            
            # Check for camera filter
            if camera_name:
                cleaned_filter = camera_name.strip().lower()
                match = re.search(r'^-\s+\[[^\]]+\]\s+\[([^\]]+)\]', line)
                if match:
                    entry_camera = match.group(1).strip().lower()
                    if cleaned_filter not in entry_camera and entry_camera not in cleaned_filter:
                        continue
                else:
                    if cleaned_filter not in line.lower():
                        continue
            
            entries.append(line.strip())
            
        if not entries:
            filter_msg = f" for camera '{camera_name}'" if camera_name else ""
            return f"No security camera logs found{filter_msg}."
            
        # Get the latest 'limit' entries
        recent_entries = entries[-limit:]
        result_str = "\n".join(recent_entries)
        return f"Recent Security Camera Logs:\n{result_str}"
    except Exception as e:
        logging.error(f"❌ CAMERA LOG: Error reading security log: {e}", exc_info=True)
        return f"Failed to retrieve security logs: {e}"

def get_camera_log_summary() -> str:
    """
    Returns a brief, one-line summary of recent camera activity (within the last hour).
    Called by helper functions to inject a hint into the system prompt.
    """
    if not os.path.exists(CAMERA_LOG_FILE_PATH):
        return ""
        
    try:
        from datetime import datetime, timedelta
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
            if match:
                time_str = match.group(1)
                camera = match.group(2).strip()
                try:
                    entry_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                    naive_now = now_dt.replace(tzinfo=None)
                    naive_one_hour_ago = one_hour_ago.replace(tzinfo=None)
                    
                    if naive_one_hour_ago <= entry_dt <= naive_now:
                        event_count += 1
                        cameras_seen.add(camera)
                except Exception:
                    pass
                    
        if event_count == 0:
            return ""
            
        camera_word = "camera" if len(cameras_seen) == 1 else "cameras"
        camera_list = ", ".join(sorted(cameras_seen))
        event_word = "event" if event_count == 1 else "events"
        return f"{event_count} security camera {event_word} detected in the last hour across {len(cameras_seen)} {camera_word} ({camera_list})"
    except Exception as e:
        logging.error(f"❌ CAMERA LOG: Error summarizing logs: {e}", exc_info=True)
        return ""

# --- CONVERSATIONAL TOPIC MONITOR ---

_is_summarizing_topics = False
_last_summary_hist_len = {}

async def summarize_topics_background(chat_id: int, user_id: int = None) -> None:
    """
    Summarizes the recent chat history topics and appends them
    to the Raw Memory Intake staging area of memory.md.
    """
    global _is_summarizing_topics, _last_summary_hist_len
    import emery.globals as globals
    from datetime import datetime
    
    if user_id is None:
        user_id = globals.current_user_id.get()
    
    # Get active history
    history = list(globals.chat_histories.get(chat_id, []))
    hist_len = len(history)
    
    # Debounce check: only run if history length increased by at least 2 messages (one full turn)
    last_len = _last_summary_hist_len.get(chat_id, 0)
    if hist_len - last_len < 2:
        return
        
    if _is_summarizing_topics:
        return
        
    _is_summarizing_topics = True
    try:
        # Only check the last 6 messages (3 turns)
        recent_history = history[-6:]
        snippet = []
        for msg in recent_history:
            role = msg.get("role")
            content = msg.get("content", "")
            # Strip think tags or other system triggers
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE).strip()
            if not content.startswith("[System"):
                snippet.append(f"{role.upper()}: {content}")
                
        if not snippet:
            return
            
        snippet_text = "\n".join(snippet)
        
        # Current localized date
        now_dt = datetime.now(USER_TIMEZONE)
        now_str = now_dt.strftime("%A, %B %d, %Y")
        
        chat_type = "DM" if chat_id > 0 else "Group Chat"
        
        system_prompt = (
            "You are Emery's Conversation Topic Summarizer.\n"
            "Your job is to read a recent snippet of the conversation and write a single, brief bullet point "
            "summarizing the main topic(s) discussed, including the date and the channel type (DM or Group Chat), "
            "AND append 3-5 high-level conceptual keywords/tags in brackets at the end.\n"
            "Example format:\n"
            f"- On {now_str} (in {chat_type}): Discussed SpaceX IPO valuations and compared it to OpenAI. [Tags: space exploration, rocket, investment, tech ipo]\n"
            "Rules:\n"
            "1. Focus ONLY on the topics/subject matters discussed (what was the chat about), not details, conversations, or decisions.\n"
            "2. Keep it to a single, concise bullet point (maximum 1 sentence).\n"
            f"3. Explicitly state that this occurred in the {chat_type} as shown in the example format.\n"
            "4. Provide 3 to 5 broad concept tags inside brackets at the very end. The tags must help group the topic conceptually (e.g. if the topic is NASA Artemis, tags should include space exploration, moon, rocket).\n"
            "5. If the recent snippet is just generic greeting, small talk, or tool command status with no real topic discussed, reply with exactly 'NONE'.\n"
            "6. Do not include conversational remarks, explanations, or code block formatting."
        )
        
        user_prompt = f"Here is the recent conversation snippet:\n\n{snippet_text}"
        
        from emery.helpers import query_fast_model
        summary = await query_fast_model(user_prompt, system_prompt)
        summary = summary.strip()
        
        if summary and summary.upper() != "NONE" and (summary.startswith("-") or summary.startswith("*")):
            # Extract raw text from bullet to pass to save_user_memory
            raw_fact = summary.lstrip("-* ").strip()
            logging.info(f"💾 TOPIC MONITOR: Identified new topic: '{raw_fact}'")
            # Save it to raw intake staging area (which triggers memory consolidation automatically)
            await save_user_memory(raw_fact, user_id)
            
            # Update last checked history length on success
            _last_summary_hist_len[chat_id] = hist_len
            
    except Exception as e:
        logging.error(f"❌ TOPIC MONITOR: Background topic summary crash: {e}", exc_info=True)
    finally:
        _is_summarizing_topics = False
