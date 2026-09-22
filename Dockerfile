FROM python:3.11-slim

WORKDIR /app

ARG APP_UID=1000
ARG APP_GID=1000

# Install ffmpeg for audio conversion and headless Chromium for browser tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg chromium \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests before application code so prompt/code edits can
# reuse the expensive Python dependency layers.
COPY requirements.txt .

# Install runtime dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application after dependencies for faster iteration.
COPY main.py .
COPY emery/ ./emery/

# Keep the gateway and supervised Chromium process non-root. Compose supplies
# the same numeric identity so host bind mounts remain writable by hudson.
RUN groupadd --gid "${APP_GID}" emery \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --home-dir /home/emery --shell /bin/sh emery \
    && mkdir -p /app/config /app/data /app/secrets \
    && chown -R "${APP_UID}:${APP_GID}" /app /home/emery

USER ${APP_UID}:${APP_GID}

# Command to run the bot
CMD ["python", "main.py"]
