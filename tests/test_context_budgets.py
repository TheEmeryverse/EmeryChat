import os
import sys
from unittest.mock import AsyncMock, Mock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emery import engine
from emery.tools import fetch_web_content


def test_main_history_compacts_old_messages_and_keeps_recent_context():
    history = [
        {"role": "user", "content": f"old message {index} " + ("x" * 80)}
        for index in range(20)
    ]
    history.extend([
        {"role": "assistant", "content": "tool request", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "name": "lookup", "tool_call_id": "call-1", "content": "tool result"},
        {"role": "user", "content": "latest question"},
    ])

    with patch.object(engine, "MAIN_MODEL_CONTEXT_TOKENS", 100), \
         patch.object(engine, "CONTEXT_COMPACTION_THRESHOLD", 0.70):
        compacted = engine._compact_history_for_model(history)

    assert compacted[0]["role"] == "system"
    assert "compacted" in compacted[0]["content"]
    assert compacted[-1]["content"] == "latest question"
    assert not (compacted[1]["role"] == "tool")


def test_fast_web_summary_is_capped_before_request():
    response = Mock()
    response.status_code = 200
    response.text = "<html><head><title>Long page</title></head><body>" + ("important fact " * 20000) + "</body></html>"
    response.headers = {"content-type": "text/html; charset=utf-8"}
    response.url = "https://example.com/long"

    with patch("emery.tools._validate_fetch_url", new=AsyncMock(return_value=(True, ""))), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response), \
         patch("emery.tools.query_fast_model", new=AsyncMock(return_value="compact summary")) as summarize:
        result = __import__("asyncio").run(fetch_web_content("https://example.com/long", max_chars=8000))

    assert result["success"] is True
    prompt = summarize.await_args.args[0]
    assert len(prompt) <= 4 * (int(16384 * 0.70) - 1024) + 100
    assert result["content"].startswith("[Summarized by Coprocessor]")
