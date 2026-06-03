# EmeryChat

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey.svg)]()
[![Docker Support](https://img.shields.io/badge/docker-ready-cyan.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()

EmeryChat is a Telegram-first personal assistant that runs against local or self-hosted AI models and can be extended with practical tools like weather, calendar, finance, Nest, Reolink, Portainer, RSS, image generation, voice, and scheduled jobs.

The project is built around a simple operating model:

- Telegram is the UI.
- A primary model handles normal conversation and tool orchestration.
- A secondary fast model can handle vision, summarization, memory cleanup, and delegated sub-tasks.
- Long-lived state is kept in local files instead of bloating every prompt.

## What It Does

- Runs as a Telegram bot with support for text, photos, voice messages, stickers, GIFs, and message reactions
- Supports local or OpenAI-compatible chat endpoints through Ollama/Open WebUI-style APIs
- Maintains persistent local memory in `memory.md`, plus per-user memory files for a second user when configured
- Supports family/group-chat behavior with silent listening, debounced replies, and user-specific memory
- Can schedule one-off or recurring jobs and send the results back into Telegram
- Can route chat, routines, and security alerts into separate Telegram forum topics
- Supports optional tools for:
  - Google Calendar
  - Google Nest thermostat control
  - NOAA weather with saved aliases like `home` and `work`
  - RSS/news, NASA APOD, and Today in History
  - Finance and macro data via FRED, IMF DataMapper, and Alpha Vantage
  - Reolink camera snapshots and polling alerts
  - Portainer environment/container management
  - Web search and web content extraction
  - Voice output, speech-to-text, and image generation

## Project Layout

```text
.
├── main.py                     # Telegram app entrypoint
├── setup_emery.py              # Interactive first-run setup wizard
├── emery/
│   ├── bot.py                  # Telegram handlers and debounce flow
│   ├── config.py               # Environment loading and config helpers
│   ├── engine.py               # Model/tool orchestration
│   ├── helpers.py              # Prompting, formatting, delegation helpers
│   ├── memory.py               # Persistent memory read/write/consolidation
│   ├── scheduler.py            # Custom job persistence and job queue wiring
│   └── tools.py                # Tool implementations
├── config/                     # Auto-generated persistent JSON config/state
├── example.env                 # Environment template for secrets and toggles
├── memory.md                   # Primary user memory file
├── camera_log.md               # Camera/security log storage
├── Dockerfile
└── docker-compose.yml
```

## Architecture

### Chat flow

1. Telegram delivers a message to `main.py`.
2. [emery/bot.py](/Users/hudson/Documents/GitHub/EmeryChat/emery/bot.py) normalizes the input, updates chat history, and applies group-chat reply rules.
3. Debounce logic batches rapid-fire messages into a single turn.
4. [emery/engine.py](/Users/hudson/Documents/GitHub/EmeryChat/emery/engine.py) builds the prompt, registers the enabled tools, and calls the main model.
5. Tool results and the final response are posted back to Telegram.

### Memory model

- Persistent facts live in `memory.md`.
- New facts are staged into a raw intake section first.
- The fast model can consolidate memory in the background to deduplicate and organize it.
- If a secondary user is configured, their memory file name is derived from `MEMORY_FILE_PATH` and the secondary user name in `config/users.json`.
  - Example: `memory.md` + secondary user `Alex Smith` becomes `memory_alex_smith.md`.

### Multi-user behavior

- In DMs, EmeryChat replies normally.
- In group chats, EmeryChat records all messages for context but only replies when:
  - it is mentioned,
  - someone replies to one of its messages, or
  - the message is a slash command.
- Per-user memory and scheduled reminders can target one user or both users.

## Quick Start

### 1. Create the Telegram bot

1. Open [@BotFather](https://t.me/botfather).
2. Run `/newbot`.
3. Copy the token into `TELEGRAM_TOKEN`.
4. Send the bot one message after startup so it has a live chat to reply into.

### 2. Prepare models and services

At minimum, EmeryChat needs:

- a chat-completions endpoint for the main model
- optionally a second endpoint/model for fast vision and background work

Typical local setup:

```bash
ollama pull qwen3.6:35b-a3b
ollama pull gemma4:e4b
```

By default the app expects Ollama-compatible chat endpoints such as:

- `OLLAMA_URL=http://localhost:11434/api/chat`
- `VISION_OLLAMA_URL=http://localhost:11434/api/chat`

Optional services:

- STT endpoint for voice transcription
- Kokoro-compatible TTS endpoint for voice replies
- SearXNG for web search

### 3. Configure the environment

```bash
cp example.env .env
python setup_emery.py
```

If you have an older personal `.env` from the pre-migration architecture, you can seed the wizard from it:

```bash
python setup_emery.py --import-env /path/to/your/old.env
```

The setup wizard will use that file as defaults, ask you to confirm/update values, then write the new `.env` and JSON config files.

Important:

- `.env` is now intentionally slim. It should hold secrets, URLs, and top-level toggles.
- EmeryChat auto-generates a persistent `config/` directory on startup for structured app-owned JSON.
- Users should not need to create or manually edit those JSON files in normal use.

### 4. Run locally

Install Python 3.10+ and `ffmpeg`, then:

```bash
python -m venv venv
source venv/bin/activate
pip install "python-telegram-bot[job-queue]" httpx requests Pillow feedparser psutil pytz tghtml markdown python-dotenv google-api-python-client google-auth-httplib2 google-auth-oauthlib beautifulsoup4
python setup_emery.py
python main.py
```

Notes:

- There is currently no `requirements.txt`; the command above mirrors the project dependencies used by the app and container.
- `ffmpeg` is required for voice output conversion.

### 5. Run with Docker Compose

Before starting Docker, create the bind-mounted files on the host so Docker does not replace them with directories:

```bash
mkdir -p config
touch memory.md token.json nest_token.json credentials.json nest_credentials.json
```

Then start the stack:

```bash
docker compose up --build -d
docker compose logs -f
```

Important Docker note:

- The entire `config/` directory is bind-mounted, so app-managed JSON survives restarts, rebuilds, and new image pulls.
- If you use a secondary user and want their derived memory file persisted in Docker, add a bind mount for that specific derived filename.

## Google Authentication

Google Calendar and Nest require OAuth credentials.

1. Create a Google Cloud project.
2. Enable the APIs you need:
   - Google Calendar API
   - Smart Device Management API for Nest
3. Create a desktop OAuth client.
4. Place the downloaded client JSON files in the repo root:
   - `credentials.json` for Calendar
   - `nest_credentials.json` for Nest
5. Run:

```bash
python generate_google_token.py
```

Then:

- choose option `1` for Calendar, which creates `token.json`
- choose option `2` for Nest, which creates `nest_token.json`

## Telegram Commands

Built-in slash commands:

- `/clear` clears the active chat context history
- `/wipe` resets the current user's persistent memory file

Most other behavior is natural-language driven through the model and enabled tools.

## Tool Catalog

### Core interaction

- Text chat
- Photo input with vision description
- Voice-message transcription
- Sticker/GIF logging and sending
- Message reactions
- Thread-aware replies

### Personal context and memory

- Persistent local memory in `memory.md`
- Memory wipe and consolidation
- Cross-chat recent-topic recall
- Secondary-user segmented memory

### Scheduling and automation

- `add_scheduled_job`
- `list_scheduled_jobs`
- `remove_scheduled_job`

Supported schedules:

- `daily` like `08:30`
- `interval` like `30m` or `1h`
- `once` like `2026-06-02 18:00:00` or `15m`
- `weekly` like `Monday 08:30`
- `monthly` like `1 12:00`
- `yearly` like `12-19 08:30`

### Information and research

- Web search via SearXNG
- Web content extraction and summarization
- RSS headline aggregation
- NASA APOD
- Today in History

### Weather

- NOAA forecast lookup by place
- Saved weather aliases such as `home`, `work`, `school`, and `office`
- Optional alert inclusion for severe weather

### Finance

- FRED series search and observations
- IMF DataMapper indicator search and cross-country data
- Alpha Vantage stock snapshots and price history
- Curated dashboards for:
  - U.S. macro
  - markets
  - global macro

### Smart home and infrastructure

- Google Calendar agenda lookup
- Nest thermostat status and control
- Reolink camera snapshots and background polling alerts
- Portainer environment/container inspection and updates

### Media and generation

- Kokoro-compatible text-to-speech
- Image generation through Gemini
- Fast-model delegation for summarization and image tasks

## Reolink Behavior

If `ENABLE_REOLINK_POLLING=true`, EmeryChat starts a background loop after bot startup and can:

- watch for AI person-detection alerts,
- post alerts into Telegram,
- attach camera snapshots,
- reuse existing alert threads for repeated events from the same camera.

Relevant config:

- `.env`: `ENABLE_REOLINK`, `REOLINK_HOST`, `REOLINK_USER`, `REOLINK_PASSWORD`
- `config/integrations.json`: Reolink camera mappings, polling/threading behavior, Telegram routing

## Forum Topics and Routing

If your Telegram group uses Topics/Forums, EmeryChat can route messages by purpose:

- `telegram.security_topic_id` for Reolink alerts
- `telegram.routines_topic_id` for scheduled jobs and recurring briefings
- `telegram.chat_topic_id` for normal conversation, reminders, and heartbeat messages

This is optional. In a DM or a non-topic group, the bot still works.

## Key Environment Variables

The full env template lives in [example.env](/Users/hudson/Documents/GitHub/EmeryChat/example.env). These are the values most people need first.

### Required

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_TOKEN` | Telegram bot token from BotFather |
| `MODEL_ID` | Primary model name |
| `OLLAMA_URL` | Main model chat endpoint |

### Strongly recommended

| Variable | Default | Purpose |
| --- | --- | --- |
| `VISION_MODEL_ID` | `gemma4:e4b` | Fast/copilot model |
| `VISION_OLLAMA_URL` | `http://localhost:11434/api/chat` | Fast model endpoint |
| `NOAA_EMAIL` | `example@example.com` | Required for NOAA weather requests |
| `GOOGLE_TOKEN_PATH` | `token.json` | Google Calendar token file |
| `NEST_TOKEN_PATH` | `nest_token.json` | Google Nest token file |

### Memory and behavior

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENABLE_MEMORY` | `true` | Persistent memory on/off |
| `MEMORY_FILE_PATH` | `memory.md` | Base memory file path |
| `MAX_HISTORY_LEN` | `200` | In-memory chat history length |
| `CHAT_DEBOUNCE_DELAY` | `4.0` | Message batching delay |
| `TOOL_LOOP` | `15` | Max tool iterations in one turn |

### Scheduler and heartbeat

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENABLE_SCHEDULER` | `true` | Enables custom job scheduling |
| `ENABLE_HEARTBEAT` | `true` | Enables inactivity check-ins |
| `HEARTBEAT_INTERVAL_SECONDS` | `3600` | Heartbeat polling interval |
| `HEARTBEAT_SILENCE_THRESHOLD_SECONDS` | `14400` | Silence before check-in |
| `HEARTBEAT_SLEEP_START` | `21:30` | Quiet-hours start |
| `HEARTBEAT_SLEEP_END` | `03:30` | Quiet-hours end |

### Auto-generated JSON config

EmeryChat now generates and persists these files under `config/` on startup:

- `config/users.json`: user profiles, allowed Telegram user IDs, relationship data
- `config/integrations.json`: Telegram routing, calendar IDs, Nest project ID, Reolink camera mappings and behavior
- `config/news_feeds.json`: RSS feed list
- `config/weather_locations.json`: saved weather aliases like `home` and `work`
- `config/custom_jobs.json`: scheduled jobs

These files are app-managed and should survive restarts and rebuilds when `config/` is bind-mounted in Docker.

### Optional integrations

| Variable group | Enables |
| --- | --- |
| `ENABLE_CALENDAR`, `GOOGLE_TOKEN_PATH` | Google Calendar |
| `ENABLE_NEST`, `NEST_TOKEN_PATH` | Nest thermostat |
| `ENABLE_WEATHER`, `NOAA_EMAIL` | NOAA weather |
| `ENABLE_NEWS` | RSS/news |
| `ENABLE_NASA`, `NASA_API_KEY` | NASA APOD |
| `ENABLE_SEARCH`, `SEARXNG_URL` | Web search |
| `ENABLE_WEB_SCRAPING` | Web content fetch |
| `ENABLE_FINANCE`, `FRED_API_KEY`, `ALPHA_VANTAGE_API_KEY` | Finance tools |
| `ENABLE_VOICE`, `TTS_URL`, `TTS_VOICE`, `STT_URL`, `OPEN_WEBUI_KEY` | Voice I/O |
| `ENABLE_IMAGEGEN`, `GEMINI_API_KEY`, `IMAGE_MODEL` | Image generation |
| `ENABLE_REOLINK`, `REOLINK_HOST`, `REOLINK_USER`, `REOLINK_PASSWORD` | Reolink camera tools |
| `ENABLE_PORTAINER`, `PORTAINER_URL`, `PORTAINER_API_KEY` | Portainer tools |

## Troubleshooting

### The bot starts but does not reply

- Make sure you sent it a message in DM first.
- If user whitelisting is enabled, confirm your Telegram user ID is listed in `config/users.json`.
- In a group chat, remember it only replies when mentioned, replied to, or commanded.

### Docker created folders instead of files

You likely started Docker before creating the bind-mounted files. Stop the container, replace those directories with files, then restart.

### Voice replies fail

- Confirm `ffmpeg` is installed locally, or available in the container.
- Confirm `TTS_URL` is reachable.
- Confirm `OPEN_WEBUI_KEY` is valid if your TTS/STT service requires it.

### Calendar or Nest fails

- Re-run `python generate_google_token.py`.
- Confirm the expected token files exist in the repo root.
- Make sure your OAuth app is configured correctly in Google Cloud.
