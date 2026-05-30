FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for audio conversion
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy the script and requirements
COPY main.py .
COPY emery/ ./emery/

# Install all dependencies including python-dotenv
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    apscheduler \
    httpx \
    requests \
    Pillow \
    feedparser \
    psutil \
    pytz \
    markdown \
    python-dotenv \
    google-api-python-client \
    google-auth-httplib2 \
    google-auth-oauthlib

# Expose HTTP port
EXPOSE 8000

# Command to run the bot
CMD ["python", "main.py"]
