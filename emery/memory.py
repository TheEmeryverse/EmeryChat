import os
import re
import logging
import asyncio

from emery.config import (
    MEMORY_FILE_PATH, MEMORY_THRESHOLD, USER_NAME, USER_LOCATION,
    USER_TIMEZONE, USER_BIRTHDAY, USER_FAMILY, USER_PROFESSION
)

def retrieve_relevant_memories(user_query: str) -> str:
    """
    Reads memory.md and performs keyword filtering against the user's latest query
    to load only relevant memories, keeping the CPU-only prompt evaluation window small.
    """
    if not os.path.exists(MEMORY_FILE_PATH):
        return ""
        
    try:
        with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
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
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                current_section = stripped.lower()
                
            # Keep header sections intact
            if current_section in ["## user profile & preferences", "## project & system context"]:
                profile_context_lines.append(line)
            # Route general facts to a list we will filter
            elif current_section in ["## general facts & logs", "## raw memory intake"]:
                # Keep section headers, but only filter bullets
                if stripped.startswith("## ") or not stripped:
                    general_facts_lines.append(line)
                elif stripped.startswith("- ") or stripped.startswith("* "):
                    general_facts_lines.append(line)
            else:
                # Outside major sections (like main title)
                if not stripped.startswith("## "):
                    profile_context_lines.append(line)
                    
        # Tokenize user query to extract keywords
        # 1. Clean query (lowercase, remove punctuation)
        clean_query = re.sub(r'[^\w\s]', '', user_query.lower())
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
        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        
        if not keywords:
            # If no significant keywords found, just return the profile sections to save space
            return "\n".join(profile_context_lines)
            
        # 3. Scan general facts and keep matching lines
        matched_facts = []
        for line in general_facts_lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                # It's a fact bullet, check for keyword match
                lower_fact = stripped.lower()
                if any(kw in lower_fact for kw in keywords):
                    matched_facts.append(line)
            else:
                # Keep structure/spacing
                matched_facts.append(line)
                
        # Combine profile context with matched facts
        final_memories = profile_context_lines + ["\n## Relevant Recalled Memories"] + matched_facts
        return "\n".join(final_memories)
        
    except Exception as e:
        logging.error(f"❌ MEMORY ENGINE: Error retrieving memories: {e}", exc_info=True)
        return ""

async def save_user_memory(fact: str) -> str:
    """
    Saves a new fact, preference, or critical piece of information about the user or their environment
    to the persistent memory log. Use when the user shares something they expect you to remember long-term.
    """
    logging.info(f"💾 MEMORY: Appending new fact to staging area: '{fact}'")
    if not os.path.exists(MEMORY_FILE_PATH):
        # Create default if missing
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("# Emery's Memory Log\n\n## User Profile & Preferences\n\n## General Facts & Logs\n\n## Raw Memory Intake\n")

    try:
        # Append to the Raw Memory Intake section of memory.md
        with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
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
        
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(updated_content)
            
        # Trigger background consolidation using the fast model
        logging.info("💾 MEMORY: Scheduling background memory consolidation...")
        asyncio.create_task(consolidate_memory_background())
        
        return f"Successfully saved to memory log staging: '{fact}'"
        
    except Exception as e:
        logging.error(f"❌ MEMORY TOOL: Failed to write memory: {e}", exc_info=True)
        return f"Failed to save fact to memory: {e}"

_is_consolidating = False

async def consolidate_memory_background() -> None:
    """
    A background consolidation task that reads memory.md, runs the coprocessor model
    to deduplicate, sort, and organize the list, and then saves it.
    This keeps the main chat model from blocking on heavy processing task execution.
    """
    global _is_consolidating
    if _is_consolidating:
        logging.info("💾 CONSOLIDATOR: Memory consolidation is already in progress. Skipping duplicate run.")
        return

    logging.info("💾 CONSOLIDATOR: Starting background memory consolidation...")
    
    if not os.path.exists(MEMORY_FILE_PATH):
        logging.warning("⚠️ CONSOLIDATOR: memory.md does not exist. Aborting consolidation.")
        return
        
    _is_consolidating = True
    try:
        # Prevent concurrent file reads/writes using a simple sleep
        await asyncio.sleep(0.5)
        
        with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
            current_markdown = f.read()
            
        system_prompt = (
            "You are Emery's Memory Consolidation System. Your job is to process the memory log (written in Markdown) "
            "and merge any new facts listed under '## Raw Memory Intake' into the main categories:\n"
            "- '## User Profile & Preferences'\n"
            "- '## General Facts & Logs'\n\n"
            "Rules:\n"
            "1. Categorize all raw facts from '## Raw Memory Intake' into their appropriate section.\n"
            "2. Completely empty/clear the '## Raw Memory Intake' section so it has no bullet points listed under it anymore.\n"
            "3. Deduplicate facts. If a new fact matches an existing one, merge them or keep the most detailed/recent one.\n"
            "4. Resolve contradictions: if a new fact directly contradicts an old one (e.g., 'User moved from NYC to Seattle'), update the profile/fact with the newer information and remove the obsolete one.\n"
            "5. Keep the exact markdown section structure. Maintain bullet points. Output ONLY a single, consolidated markdown document, starting with '# Emery's Memory Log'. Do not duplicate the document, repeat the headers, or output 'before' and 'after' versions. Do not include conversational remarks, explanations, or code block formatting like ```markdown."
        )
        
        user_prompt = f"Here is the current memory file content:\n\n{current_markdown}\n\nPlease consolidate it now."
        
        # Local import to break circular dependency
        from emery.helpers import query_fast_model
        consolidated = await query_fast_model(user_prompt, system_prompt)
        
        if not consolidated or not consolidated.startswith("# Emery's Memory Log"):
            logging.error(f"❌ CONSOLIDATOR: Fast model returned invalid markdown. Aborting overwrite. Response: '{consolidated[:200]}...'")
            return
            
        # Safety Check: if the model duplicated the document, keep only the first document block
        if consolidated.count("# Emery's Memory Log") > 1:
            logging.warning("⚠️ CONSOLIDATOR: Model output contained multiple document blocks. Keeping only the first one.")
            consolidated = "# Emery's Memory Log" + consolidated.split("# Emery's Memory Log")[1]
            
        # Safety check: make sure the Raw Memory Intake section exists
        if "## Raw Memory Intake" not in consolidated:
            consolidated += "\n\n## Raw Memory Intake\n"
            
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(consolidated)
            
        logging.info("💾 CONSOLIDATOR: Background memory consolidation completed successfully!")
        
    except Exception as e:
        logging.error(f"❌ CONSOLIDATOR: Background task crash: {e}", exc_info=True)
    finally:
        _is_consolidating = False

def wipe_memory() -> bool:
    """
    Overwrites memory.md with the default baseline template structure,
    clearing all custom saved facts and preferences.
    """
    logging.info("🧠 MEMORY: Wiping all memories and restoring baseline template...")
    baseline_template = (
        f"# Emery's Memory Log\n\n"
        f"## User Profile & Preferences\n"
        f"- Name: {USER_NAME}\n"
        f"- Location: {USER_LOCATION}\n"
        f"- Timezone: {USER_TIMEZONE}\n"
        f"- Birthday: {USER_BIRTHDAY}\n"
        f"- Family: {USER_FAMILY}\n"
        f"- Profession: {USER_PROFESSION}\n\n"
        f"## General Facts & Logs\n\n"
        f"## Raw Memory Intake\n"
    )
    try:
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(baseline_template)
        return True
    except Exception as e:
        logging.error(f"❌ WIPE MEMORY: Failed to wipe memory file: {e}", exc_info=True)
        return False
