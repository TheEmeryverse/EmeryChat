import os
import logging
import emery.globals as globals

async def send_responses_to_webhook(chat_id: str, responses: list, thinking: str = None) -> bool:
    """
    Sends accumulated responses (text/photo/voice) and optional reasoning
    to the configured WEBHOOK_URL.
    """
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        logging.warning("⚠️ WEBHOOK: WEBHOOK_URL is not configured in .env. Outgoing payload dropped.")
        return False

    payload = {
        "chat_id": str(chat_id),
        "thinking": thinking,
        "responses": responses
    }

    try:
        logging.info(f"📤 WEBHOOK: Sending {len(responses)} response(s) to {webhook_url}...")
        r = await globals.http_client.post(webhook_url, json=payload, timeout=30.0)
        if r.status_code in (200, 201, 202, 204):
            logging.info("✅ WEBHOOK: Successfully delivered payload.")
            return True
        else:
            logging.error(f"❌ WEBHOOK: Webhook server returned status {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        logging.error(f"❌ WEBHOOK: Failed to send request to webhook: {e}")
        return False
