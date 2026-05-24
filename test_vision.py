import json

b64_data = "base64string12345"
user_caption = "caption here"
payload = {
    "model": "gemma4:e4b",
    "messages": [
        {
            "role": "user",
            "content": "What is this image? " + (user_caption or ""),
            "images": [b64_data]
        }
    ],
    "stream": False
}

print(json.dumps(payload, indent=2))
