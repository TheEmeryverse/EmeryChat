FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for audio conversion
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy the script and requirements
COPY main.py .

# Install all dependencies including python-dotenv
RUN pip install --no-cache-dir \
    "python-telegram-bot[job-queue]" \
    httpx \
    requests \
    Pillow \
    feedparser \
    psutil \
    pytz \
    tghtml \
    markdown \
    python-dotenv \
    google-api-python-client \
    google-auth-httplib2 \
    google-auth-oauthlib

# Command to run the bot
CMD ["python", "main.py"]
