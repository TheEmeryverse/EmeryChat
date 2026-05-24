import json

msg = {
    "content": [
        {"type": "text", "text": "Current Time: Saturday"},
        {"type": "text", "text": "Describe this image in detail."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,12345"}}
    ]
}

clean_msg = msg.copy()
if isinstance(msg.get("content"), list):
    text_parts = []
    image_parts = []
    for part in msg["content"]:
        if part.get("type") == "text":
            text_parts.append(part["text"])
        elif part.get("type") == "image_url":
            raw_url = part["image_url"]["url"]
            if "," in raw_url:
                b64_data = raw_url.split(",", 1)[1]
            else:
                b64_data = raw_url
            image_parts.append(b64_data)
    
    clean_msg["content"] = " ".join(text_parts)
    if image_parts:
        clean_msg["images"] = image_parts

print(json.dumps(clean_msg, indent=2))
