from __future__ import annotations

import json
import os


DEFAULT_POLICY = {
    "portal:interactive": 0,
    "jellyfin:interactive": 0,
    "portal:content": 10,
    "emerychat:content": 10,
}


def priority_for(source: str, request_type: str) -> int:
    """Only source/type policy entries affect priority; caller priority is ignored."""
    policy = dict(DEFAULT_POLICY)
    raw = os.environ.get("ROUTER_PRIORITY_POLICY", "")
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                for key, value in override.items():
                    if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool):
                        policy[key] = value
        except json.JSONDecodeError:
            pass
    return policy.get(f"{source}:{request_type}", max(policy.values(), default=10))
