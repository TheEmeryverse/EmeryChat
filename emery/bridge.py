"""
Inter-Agent Bridge Implementation

Enables bidirectional communication between Hermes and EmeryChat bots.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import emery.globals as globals
from emery.bridge_protocol import (
    HERMES_PREFIX,
    EMERY_PREFIX,
    format_bridge_message,
    parse_bridge_message,
    CMD_QUERY,
    CMD_RESULT,
    CMD_STATUS,
    CMD_HEARTBEAT,
    BRIDGE_MAX_MESSAGE_LENGTH
)
from emery.config import ALLOWED_BOT_IDS

class InterAgentBridge:
    """Manages inter-agent communication between Hermes and EmeryChat."""
    
    def __init__(self):
        self.bot = globals.application_bot
        self.last_message_ids = {
            "HERMES": 0,
            "EMERY": 0
        }
        self.message_history = {}
        self.is_active = False
    
    async def start(self):
        """Start the bridge."""
        self.is_active = True
        logging.info("🔗 INTER-AGENT BRIDGE: Started")
    
    async def stop(self):
        """Stop the bridge."""
        self.is_active = False
        logging.info("🔗 INTER-AGENT BRIDGE: Stopped")
    
    async def poll_for_messages(self, context: ContextTypes.DEFAULT_TYPE):
        """Poll for new messages from inter-agent accounts."""
        if not self.is_active:
            return
        
        try:
            # Get recent messages from allowed bots
            updates = await self.bot.get_updates(limit=10, timeout=1)
            
            for update in updates:
                if update.effective_user and update.effective_user.id in ALLOWED_BOT_IDS:
                    await self.handle_inter_agent_update(update, context)
                    
        except Exception as e:
            logging.error("🔗 INTER-AGENT BRIDGE: Error polling: %s", e)
    
    async def handle_inter_agent_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle an update from an inter-agent account."""
        if not update.message:
            return
        
        sender_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Check if this is a bridge message
        text = update.message.text
        if not text:
            return
        
        parsed = parse_bridge_message(text)
        if not parsed:
            # Not a bridge message, ignore
            return
        
        sender = parsed["sender"]
        cmd = parsed["cmd"]
        content = parsed["content"]
        
        logging.info(
            "🔗 INTER-AGENT BRIDGE: %s -> %s | %s | %s",
            sender,
            "EMERY" if sender == "HERMES" else "HERMES",
            cmd,
            content[:50]
        )
        
        # Route based on command type
        if cmd == CMD_QUERY:
            await self.handle_query(sender, content, update, context)
        elif cmd == CMD_STATUS:
            await self.handle_status(sender, update)
        elif cmd == CMD_HEARTBEAT:
            await self.handle_heartbeat(sender, update)
        elif cmd == CMD_RESULT:
            await self.handle_result(sender, content)
    
    async def handle_query(self, sender: str, content: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle a query from another agent."""
        # Store the query
        query_id = f"{sender}_query_{datetime.now().timestamp()}"
        self.message_history[query_id] = {
            "sender": sender,
            "content": content,
            "timestamp": datetime.now(),
            "status": "pending"
        }
        
        # Process through the engine (similar to handle_message)
        # For now, we'll just store it - the actual processing happens in handle_message
        # This is a simplified version - in production, you'd want to trigger the engine
        
        logging.info(
            "🔗 INTER-AGENT BRIDGE: Query from %s: %s",
            sender,
            content[:100]
        )
        
        # Send a placeholder response
        response = await self.bot.send_message(
            chat_id=update.effective_chat.id,
            text=format_bridge_message("EMERY", CMD_RESULT, f"Processing: {content[:100]}...")
        )
    
    async def handle_status(self, sender: str, update: Update):
        """Handle a status check."""
        status = {
            "bot_id": self.bot.id,
            "uptime": datetime.now().isoformat(),
            "active_sessions": len(self.message_history)
        }
        
        status_str = f"Status: Active | Sessions: {len(self.message_history)}"
        await self.bot.send_message(
            chat_id=update.effective_chat.id,
            text=format_bridge_message("EMERY", CMD_STATUS, status_str)
        )
    
    async def handle_heartbeat(self, sender: str, update: Update):
        """Handle a heartbeat."""
        await self.bot.send_message(
            chat_id=update.effective_chat.id,
            text=format_bridge_message("EMERY", CMD_HEARTBEAT, f"OK at {datetime.now().isoformat()}")
        )
    
    async def handle_result(self, sender: str, content: str):
        """Handle a result from another agent."""
        logging.info(
            "🔗 INTER-AGENT BRIDGE: Result from %s: %s",
            sender,
            content[:100]
        )
        
        # Find the corresponding query
        for query_id, query_data in self.message_history.items():
            if query_data["sender"] == sender and query_data["status"] == "pending":
                query_data["status"] = "completed"
                break
    
    async def send_message(self, target_sender: str, cmd: str, content: str) -> bool:
        """Send a message to another agent."""
        if not self.is_active:
            return False
        
        try:
            # Determine target chat based on sender
            if target_sender == "HERMES":
                target_chat_id = 8726427681  # Hermes bot ID
            elif target_sender == "EMERY":
                target_chat_id = 8725929278  # Emery bot ID
            else:
                logging.error("🔗 INTER-AGENT BRIDGE: Unknown target: %s", target_sender)
                return False
            
            # Truncate if necessary
            if len(content) > BRIDGE_MAX_MESSAGE_LENGTH:
                content = content[:BRIDGE_MAX_MESSAGE_LENGTH - 3] + "..."
            
            message = format_bridge_message(target_sender, cmd, content)
            
            await self.bot.send_message(
                chat_id=target_chat_id,
                text=message
            )
            
            logging.info(
                "🔗 INTER-AGENT BRIDGE: Sent to %s: %s",
                target_sender,
                content[:50]
            )
            
            return True
            
        except Exception as e:
            logging.error("🔗 INTER-AGENT BRIDGE: Error sending to %s: %s", target_sender, e)
            return False


# Singleton instance
bridge = InterAgentBridge()
