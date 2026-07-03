"""
Inter-Agent Bridge Protocol

Defines the message format for bot-to-bot communication.
"""

# Bridge message prefixes
HERMES_PREFIX = "[HERMES]"
EMERY_PREFIX = "[EMERY]"

# Command types
CMD_QUERY = "QUERY"
CMD_RESULT = "RESULT"
CMD_STATUS = "STATUS"
CMD_HEARTBEAT = "HEARTBEAT"

# Message structure
def format_bridge_message(sender: str, cmd: str, content: str) -> str:
    """Format a bridge message with sender, command type, and content."""
    return f"{sender} {cmd} {content}"

def parse_bridge_message(text: str) -> dict:
    """Parse a bridge message into its components."""
    if text.startswith(HERMES_PREFIX):
        sender = "HERMES"
        text = text[len(HERMES_PREFIX):].strip()
    elif text.startswith(EMERY_PREFIX):
        sender = "EMERY"
        text = text[len(EMERY_PREFIX):].strip()
    else:
        return None  # Not a bridge message
    
    parts = text.split(None, 1)
    if len(parts) < 2:
        return None
    
    cmd = parts[0]
    content = parts[1]
    
    return {
        "sender": sender,
        "cmd": cmd,
        "content": content
    }

# Default bridge configuration
BRIDGE_ENABLED = True
BRIDGE_POLL_INTERVAL = 30  # seconds
BRIDGE_MAX_MESSAGE_LENGTH = 4000  # Telegram message limit

# Command protocol
BRIDGE_COMMANDS = {
    "QUERY": "Request a query to be processed",
    "RESULT": "Return the result of a query",
    "STATUS": "Check the status of the other agent",
    "HEARTBEAT": "Heartbeat to verify connectivity"
}
