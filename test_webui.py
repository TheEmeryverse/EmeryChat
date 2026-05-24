import requests
import os
import json

key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6ImUyNDUyYmZhLTkzNDYtNDZiYy1iMGFmLWE0OWQ0NDk4OWYwNiIsImV4cCI6MTc4MTY1MDE4NCwianRpIjoiODg4OTRmMjEtMmQ5OC00N2FiLThkZDEtNjkwNThhYmQzZTU2IiwiaWF0IjoxNzc5MjMwOTg0fQ.KKGel2iEcsN6ho2MXr8cHfdHGcqNGWXKxlrSMnmCg78"
url = "http://192.168.1.121:3000/api/chat/completions"

headers = {
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json"
}

payload = {
    "model": "gemma4:e4b",
    "messages": [
        {
            "role": "user",
            "content": "Hello"
        }
    ],
    "stream": False,
    "chat_id": ""
}

try:
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    print(r.status_code)
    print(r.text)
except Exception as e:
    print(f"Error: {e}")
