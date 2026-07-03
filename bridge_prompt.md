You are an expert in Python, telegram-bot-api, and inter-agent communication protocols.

## Current State

EmeryChat runs at /home/hudson/EmeryChat with the main bot in /home/hudson/EmeryChat/emery/bot.py.
It uses python-telegram-bot with strict access control:

```python
def is_user_allowed(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not ALLOWED_USER_IDS:
        return bool(ALLOW_UNRESTRICTED_TELEGRAM_ACCESS)
    return user.id in ALLOWED_USER_IDS
```

This only allows user IDs, not bot accounts.

## Problem

I need to establish bidirectional communication between two Telegram bots:
- Hermes: bot ID 8726427681
- EmeryChat: bot ID 8725929278

Both bots are currently isolated — they cannot communicate because:
1. EmeryChat's `is_user_allowed()` only accepts user IDs, not bot IDs
2. There's no inter-agent protocol
3. Message routing between the two bots doesn't exist

## Goal

Design and implement an inter-agent bridge that enables:
1. **Bot authentication**: Separate from user authentication, verify the sender is an authorized bot
2. **Message routing**: Forward messages between the two bots
3. **Response handling**: Capture responses and route them back to the originating bot
4. **Command protocol**: Define a format for inter-agent communication

## Requirements

- Must not break existing user access control
- Must support bidirectional communication
- Should use a simple, robust protocol
- Must handle errors gracefully
- Should be minimal changes to existing code

## Files to Analyze

- `/home/hudson/EmeryChat/emery/bot.py` — main bot logic, access control, message handlers
- `/home/hudson/EmeryChat/emery/config.py` — configuration loading
- `/home/hudson/EmeryChat/config/users.json` — current whitelist (has allowed_user_ids)

## Design Constraints

- Use low creativity / deterministic approach
- Prioritize correctness over cleverness
- Follow existing code patterns in bot.py
- Minimal footprint — don't over-engineer

## Expected Output

1. **Analysis Summary**: What changes are needed in bot.py
2. **Protocol Design**: Message format and routing logic
3. **Implementation Plan**: Specific code changes with file paths and line numbers
4. **Starting Code**: The actual modified functions/code blocks

Please analyze the code and provide a concrete, actionable implementation plan.
