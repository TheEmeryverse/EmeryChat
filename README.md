# EmeryChat

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey.svg)]()
[![Docker Support](https://img.shields.io/badge/docker-ready-cyan.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()

EmeryChat is a Telegram-first personal assistant that runs against local or self-hosted AI models and can be extended with practical tools like weather, calendar, finance, Nest, Reolink, Portainer, RSS, image generation, voice, and scheduled jobs.

The project is built around a simple operating model:

- Telegram is the UI.
- A primary model handles normal conversation and tool orchestration.
- A fast text coprocessor can handle summarization, memory cleanup, and delegated sub-tasks.
- A separate vision model is only needed for multimodal inputs and camera/security image analysis.
- Long-lived state is kept in local files instead of bloating every prompt.

## What It Does

- Runs as a Telegram bot with support for text, photos, voice messages, stickers, GIFs, and message reactions
- Supports local or OpenAI-compatible chat endpoints through Ollama/Open WebUI-style APIs
- Maintains structured persistent memory in `data/memory/memory_store.json`
- Supports family/group-chat behavior with silent listening, debounced replies, scoped memory ownership, and user-specific recall
- Can schedule one-off or recurring jobs and send the results back into Telegram
- Supports `/expert` foreground research sessions with multi-round source gathering, optional read-only econ/finance tools, rich reports, archive, and resume
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
├── scripts/                    # Setup and one-off utility scripts
│   ├── setup_emery.py          # Interactive first-run setup wizard
│   └── generate_google_token.py # OAuth token helper
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
├── data/                       # Runtime state like memory store and logs
├── secrets/                    # Local secret material
│   └── google/                 # Google OAuth credentials and generated tokens
├── backups/                    # Setup-script backups of overwritten files
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

- Persistent memory lives in `data/memory/memory_store.json` as structured records with owner, scope, and visibility metadata.
- The embedding model can rank semantically relevant memories, while lexical fallback still works if embeddings are unavailable.
- Group-chat topic memory is stored separately from private user memory so public context does not automatically leak into DM recall.
- Topic summarization now asks the fast model for strict JSON, then validates and normalizes the result before storing it.

### Topic Debugging

- Set `LOG_LEVEL=DEBUG` to inspect topic-memory processing.
- `TOPIC DEBUG [raw_model_payload]` shows the parsed JSON returned by the fast model.
- `TOPIC DEBUG [normalized_topics]` shows the cleaned topic list after schema enforcement and dedupe.
- `TOPIC DEBUG [final_topic_payload]` shows the final stored payload with resolved ownership and chat scope.

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
- optionally separate fast-text, vision, and embedding endpoints/models

Typical local setup:

```bash
ollama pull gpt-oss:20b
ollama pull qwen3.6:35b-a3b
ollama pull gemma4:e4b
ollama pull lfm2.5:8b
ollama pull minicpm4.5:8b
ollama pull nomic-embed-text
```

Recommended local model roles:

- `gpt-oss:20b`: excellent primary model, especially strong at tool calling and routine orchestration.
- `qwen3.6:35b-a3b`: capable larger primary model option for richer conversational turns and final synthesis.
- `gemma4:e4b`: good vision model and great fast text model for delegated cleanup, summarization, and lightweight extraction.
- `lfm2.5:8b`: excellent fast text model for coprocessor work and read-only tool preflight when you want speed with strong instruction following.
- `minicpm4.5:8b`: vision model for image descriptions and camera/security image analysis.
- `nomic-embed-text`: embedding model for semantic memory retrieval.

By default the app expects a primary OpenAI-compatible chat-completions endpoint plus configurable secondary endpoints. A common local split is:

- `MAIN_MODEL_URL=http://127.0.0.1:8081/v1/chat/completions`
- `FAST_MODEL_URL=http://127.0.0.1:8082/v1/chat/completions`
- `VISION_OLLAMA_URL=http://localhost:11434/api/chat`
- `EMBEDDING_OLLAMA_URL=http://localhost:11434/api/embed`

The fast text coprocessor uses an OpenAI-compatible chat-completions endpoint. For a local llama.cpp server, point it at `FAST_MODEL_URL=http://127.0.0.1:8082/v1/chat/completions`. When `AGENTIC_FAST_TOOLS_ENABLED=true`, the fast model gets a read-only subset of the tool schema before each main-model turn and may prefetch useful context, such as web results, transcripts, weather, market data, camera lists, or camera security logs. Mutating tools and tools that send chat media are explicitly denied in this path.

#### llama.cpp main-model backend

The main model path is optimized for a local `llama-server` OpenAI-compatible endpoint. EmeryChat keeps the reusable prompt prefix stable and keeps chat history append-only so llama.cpp can restore prompt-cache checkpoints instead of reprocessing the full prompt each turn.

For Qwen3.6 GGUF models on llama.cpp, use prompt caching and full SWA cache support. A known-good baseline:

```bash
./build/bin/llama-server \
  -m /path/to/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
  --host 0.0.0.0 \
  --port 8081 \
  --device Vulkan1 \
  --split-mode none \
  --ctx-size 65536 \
  --parallel 1 \
  --gpu-layers 999 \
  --cpu-moe \
  --no-mmap \
  --kv-offload \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  --batch-size 1024 \
  --ubatch-size 256 \
  --flash-attn auto \
  --swa-full \
  --cache-prompt \
  --cache-reuse 64 \
  --ctx-checkpoints 256 \
  --checkpoint-min-step 32 \
  --cache-ram -1 \
  --cache-idle-slots
```

Keep these details intact:

- Use the OpenAI-compatible endpoint: `http://127.0.0.1:8081/v1/chat/completions`.
- Set `MODEL_ID=local` unless your llama.cpp server expects a different model name.
- Keep `--swa-full` for Qwen3.6/SWA models; without it, llama.cpp may invalidate checkpoints and force full prompt reprocessing.
- Keep `--cache-prompt`, `--cache-reuse`, `--ctx-checkpoints`, and `--checkpoint-min-step` enabled for prompt reuse.
- Do not wrap or split long flags accidentally; for example, `--ctx-checkpoints` and `--cache-idle-slots` must remain single flags.

Healthy llama.cpp logs should show checkpoint restoration and a small suffix eval after the first turn:

```text
restored context checkpoint ...
prompt eval time = ... / small-number-of-tokens
```

Avoid repeated logs like:

```text
forcing full prompt re-processing due to lack of cache data
```

EmeryChat logs prompt-cache diagnostics for each main-model request:

```text
PROMPT CACHE: stable_prefix_hash=... tool_schema_hash=... request_static_hash=... dynamic_messages=0 ...
```

During normal operation, `stable_prefix_hash`, `tool_schema_hash`, and `request_static_hash` should stay constant. `dynamic_messages=0` means dynamic context is appended inside the newest history event rather than inserted before reusable history.

Optional services:

- STT endpoint for voice transcription
- Kokoro-compatible TTS endpoint for voice replies
- SearXNG for web search

### 3. Configure the environment

```bash
cp example.env .env
python scripts/setup_emery.py
```

Important:

- `.env` should hold secrets, URLs, and top-level toggles.
- EmeryChat auto-generates a persistent `config/` directory on startup for structured app-owned JSON.
- `scripts/setup_emery.py` stores file backups under `backups/` when it overwrites `.env` or app-managed JSON files.
- Users should not need to create or manually edit those JSON files in normal use.

### 4. Run locally

Install Python 3.10+ and `ffmpeg`, then:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python scripts/setup_emery.py
python main.py
```

Notes:

- `ffmpeg` is required for voice output conversion.

### 5. Run with Docker Compose

Before starting Docker, create the bind-mounted files on the host so Docker does not replace them with directories:

```bash
mkdir -p config data/memory data/logs secrets/google
touch secrets/google/token.json secrets/google/nest_token.json secrets/google/credentials.json secrets/google/nest_credentials.json
```

Then start the stack:

```bash
docker compose up --build -d
docker compose logs -f
```

Important Docker note:

- The entire `config/` directory is bind-mounted, so app-managed JSON survives restarts, rebuilds, and new image pulls.
## Google Authentication

Google Calendar and Nest require OAuth credentials.

1. Create a Google Cloud project.
2. Enable the APIs you need:
   - Google Calendar API
   - Smart Device Management API for Nest
3. Create a desktop OAuth client.
4. Place the downloaded client JSON files in `secrets/google/`:
   - `secrets/google/credentials.json` for Calendar
   - `secrets/google/nest_credentials.json` for Nest
5. Run:

```bash
python scripts/generate_google_token.py
```

Then:

- choose option `1` for Calendar, which creates `secrets/google/token.json`
- choose option `2` for Nest, which creates `secrets/google/nest_token.json`

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

- Persistent local memory in `data/memory/memory_store.json`
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

Routing behavior:

- Personal reminders such as "remind me tomorrow at 8am" are sent as a DM to the asker, including recurring personal reminders.
- Shared reminders such as "remind us on June 7 at 9am" are sent to the configured group chat topic.
- True routines and automation, such as recurring briefings, monitoring, and scheduled checks, are sent to the routines topic.
- One-off reminders with a date but no time ask for a time before scheduling.

### Information and research

- `/expert <topic>` foreground deep-research sessions with multi-round source gathering, rich Telegram reports, archive, and resume
- `/expert help` shows expert-specific command help
- `/expert list` shows archived reports as topic/date buttons; selecting one opens a second button menu for `Resume`, `Open report`, or `Cancel`
- `/expert resume <id>` restores an archived session into the current chat/thread without auto-continuing research
- `/expert open <id>` sends the archived report back through the rich Telegram delivery path
- `/expert status` shows the current active expert session state
- `/expert clear` deletes all archived expert reports and clears the archive index
- `/expert cancel` cancels the active expert session in the current chat/thread
- Web search via SearXNG
- Web content extraction and summarization
- YouTube transcript extraction when `ENABLE_YOUTUBE_TRANSCRIPT=true`; expert mode uses transcripts for YouTube sources and does not count transcript failures as gathered sources
- RSS headline aggregation
- NASA APOD
- Today in History

Expert reports use citations like `[S12]` inside the report. The script sends the full source list separately, so the model is instructed not to include source appendices, source-list boilerplate, or delivery disclaimers inside the report itself.

When an expert session is closed and archived, EmeryChat writes a resumable bundle under `EXPERT_ARCHIVE_DIR`. The default is `data/expert`, which is persisted by the default Docker bind mount for `./data:/app/data`:

- `session.json`: complete session state for resume
- `report.md`: final report plus source appendix for local archive use
- `sources.json`: source metadata, fetch status, summaries, and labels
- `econ_results.json`: structured tool result metadata and summaries
- `loop.md`: readable round-by-round research log

### Weather

- NOAA forecast lookup by place
- Saved weather aliases such as `home`, `work`, `school`, and `office`
- Optional alert inclusion for severe weather

### Finance

- FRED series search and observations
- IMF DataMapper indicator search and cross-country data
- Alpha Vantage stock snapshots and price history
- Curated dashboards for:
  - bond markets
  - inflation
  - U.S. macro
  - equity market
  - housing/consumer
  - labor market
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

### Scheduled jobs and reminders

- One-off and recurring jobs are stored in `config/custom_jobs.json`.
- The saved `prompt` is the instruction that runs when the job fires.
- New scheduled jobs also preserve `source_request`, the original user message that created the job, so reminder delivery can recover the user's actual wording even if the saved prompt or description is terse.
- Reminder jobs are delivered through a structured execution prompt that includes the saved prompt, description, and original source request, then asks the model to send the reminder directly without setup chatter or follow-up questions.

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
- `telegram.routines_topic_id` for routines, recurring briefings, monitoring, and scheduled checks
- `telegram.chat_topic_id` for normal conversation, shared one-off reminders, and heartbeat messages

This is optional. In a DM or a non-topic group, the bot still works.

## Key Environment Variables

The full env template lives in [example.env](/Users/hudson/Documents/GitHub/EmeryChat/example.env). These are the values most people need first.

### Required

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_TOKEN` | Telegram bot token from BotFather |
| `MODEL_ID` | Primary model name |
| `MAIN_MODEL_URL` | Main model chat endpoint |

Telegram access is fail-closed by default. Add your Telegram user ID to `config/users.json` through the setup wizard, or explicitly set `ALLOW_UNRESTRICTED_TELEGRAM_ACCESS=true` if you want anyone who can message the bot to use it.

### Strongly recommended

| Variable | Default | Purpose |
| --- | --- | --- |
| `FAST_MODEL_ID` | `gemma4:e4b` | Fast text coprocessor model; `lfm2.5:8b` is recommended for fast tool preflight |
| `FAST_MODEL_URL` | `http://127.0.0.1:8082/v1/chat/completions` | Fast text coprocessor endpoint |
| `AGENTIC_FAST_TOOLS_ENABLED` | `true` | Allows the fast model to pre-call useful read-only tools before the main model turn |
| `AGENTIC_FAST_MAX_TOOL_CALLS` | `3` | Max read-only tool calls the fast model can prefetch in one turn |
| `AGENTIC_FAST_ALLOWED_TOOLS` | read-only built-ins | Optional comma-separated override for fast preflight tools |
| `VISION_MODEL_ID` | `gemma4:e4b` | Vision/multimodal model; use your local `minicpm4.5:8b` tag if that is your vision server model |
| `VISION_OLLAMA_URL` | `http://localhost:11434/api/chat` | Vision model endpoint |
| `EMBEDDING_MODEL_ID` | `nomic-embed-text` | Embedding model for semantic memory retrieval |
| `EMBEDDING_OLLAMA_URL` | `http://localhost:11434/api/embed` | Embedding endpoint |
| `NOAA_EMAIL` | `example@example.com` | Required for NOAA weather requests |
| `GOOGLE_TOKEN_PATH` | `secrets/google/token.json` | Google Calendar token file |
| `NEST_TOKEN_PATH` | `secrets/google/nest_token.json` | Google Nest token file |

### Memory and behavior

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALLOW_UNRESTRICTED_TELEGRAM_ACCESS` | `false` | Allows Telegram users not listed in `config/users.json` |
| `ENABLE_MEMORY` | `true` | Persistent memory on/off |
| `ENABLE_TELEGRAM_RICH_MESSAGES` | `true` | Sends model-authored text with Telegram Bot API rich Markdown and falls back to legacy HTML |
| `MEMORY_STORE_PATH` | `data/memory/memory_store.json` | Structured memory store path |
| `CHAT_DEBOUNCE_DELAY` | `4.0` | Message batching delay |
| `TOOL_LOOP` | `15` | Max tool iterations in one turn |

Chat history is append-only during runtime to preserve llama.cpp prompt-cache checkpoint reuse. This applies to normal chat turns, reaction-triggered evaluations, heartbeat evaluations, and non-routine scheduled jobs. Routine jobs are still executed in isolated one-shot contexts, but their delivered results may be compacted and deferred before they are added back to chat history so closely spaced routines do not churn the prompt cache. After the last nearby routine has finished, Emery can issue a tiny discarded warmup completion against the full chat context so the next human query does not pay the full prefill cost. Use `/clear` only when you intentionally want to reset the in-memory prompt context for that chat.

### Scheduler and heartbeat

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENABLE_SCHEDULER` | `true` | Enables custom job scheduling |
| `ROUTINE_HISTORY_DEFER_SECONDS` | `600` | Defers compact routine-history ingestion when another routine is due soon |
| `ENABLE_ROUTINE_CACHE_WARMUP` | `true` | Warms the normal chat prompt cache after the final nearby routine finishes |
| `ENABLE_HEARTBEAT` | `true` | Enables inactivity check-ins |
| `HEARTBEAT_INTERVAL_SECONDS` | `3600` | Heartbeat polling interval |
| `HEARTBEAT_SILENCE_THRESHOLD_SECONDS` | `14400` | Silence before check-in |
| `HEARTBEAT_SILENT_RETRY_SECONDS` | `3600` | Cooldown after a silent heartbeat evaluation |
| `HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS` | `14400` | Cooldown after a proactive heartbeat message |
| `HEARTBEAT_DAILY_PROACTIVE_LIMIT` | `2` | Max proactive heartbeat messages per day |
| `HEARTBEAT_SLEEP_START` | `21:30` | Quiet-hours start |
| `HEARTBEAT_SLEEP_END` | `03:30` | Quiet-hours end |

### Auto-generated JSON config

EmeryChat now generates and persists these files under `config/` on startup:

- `config/users.json`: user profiles, allowed Telegram user IDs, relationship data
- `config/integrations.json`: Telegram routing, calendar IDs, Nest project ID, Reolink camera mappings and behavior
- `config/news_feeds.json`: RSS feed list
- `config/weather_locations.json`: saved weather aliases like `home` and `work`
- `config/custom_jobs.json`: scheduled jobs
- `config/expert_sessions.json`: index of archived `/expert` research sessions

These files are app-managed and should survive restarts and rebuilds when `config/` is bind-mounted in Docker.

`config/expert_sessions.json` stores archive metadata and file paths. Archives are expected to stay under the configured `EXPERT_ARCHIVE_DIR`. Older `.env` files may use `EXPERT_ARCHIVE_DIR=~/expert`; inside Docker that resolves to `/root/expert`, which is not persisted by the default compose file. Prefer `EXPERT_ARCHIVE_DIR=data/expert` unless you explicitly mount another archive directory.

### Optional integrations

| Variable group | Enables |
| --- | --- |
| `ENABLE_CALENDAR`, `GOOGLE_TOKEN_PATH` | Google Calendar |
| `ENABLE_NEST`, `NEST_TOKEN_PATH` | Nest thermostat |
| `ENABLE_WEATHER`, `NOAA_EMAIL` | NOAA weather |
| `ENABLE_NEWS` | RSS/news |
| `ENABLE_NASA`, `NASA_API_KEY` | NASA APOD |
| `ENABLE_SEARCH`, `SEARXNG_URL` | Web search |
| `ENABLE_WEB_SCRAPING`, `ALLOW_PRIVATE_WEB_FETCH` | Web content fetch |
| `ENABLE_YOUTUBE_TRANSCRIPT` | YouTube transcript fetch for normal chat and `/expert` YouTube sources |
| `EXPERT_ARCHIVE_DIR`, `EXPERT_INDEX_PATH`, `EXPERT_DEFAULT_TARGET_SOURCES`, `EXPERT_MIN_TARGET_SOURCES`, `EXPERT_MAX_SOURCES`, `EXPERT_MAX_AGENDA_QUESTIONS`, `EXPERT_MAX_NEW_QUESTIONS`, `EXPERT_MAX_SUBTASKS_PER_QUESTION`, `EXPERT_ALLOW_MIDLOOP_QUESTIONS`, `EXPERT_MAIN_*`, `EXPERT_FAST_*` | `/expert` research archives, index, adjustable source depth, bounded agenda expansion, optional mid-loop question pauses, archive resume/open behavior, and expert-specific model tuning |
| `ENABLE_FINANCE`, `FRED_API_KEY`, `ALPHA_VANTAGE_API_KEY` | Finance tools |
| `ENABLE_VOICE`, `TTS_URL`, `TTS_VOICE`, `STT_URL`, `OPEN_WEBUI_KEY` | Voice I/O |
| `ENABLE_IMAGEGEN`, `GEMINI_API_KEY`, `IMAGE_MODEL` | Image generation |
| `ENABLE_REOLINK`, `REOLINK_HOST`, `REOLINK_USER`, `REOLINK_PASSWORD` | Reolink camera tools |
| `ENABLE_PORTAINER`, `PORTAINER_URL`, `PORTAINER_API_KEY` | Portainer tools |

## Troubleshooting

### The bot starts but does not reply

- Make sure you sent it a message in DM first.
- Confirm your Telegram user ID is listed in `config/users.json`, or explicitly set `ALLOW_UNRESTRICTED_TELEGRAM_ACCESS=true`.
- In a group chat, remember it only replies when mentioned, replied to, or commanded.

### Web fetch refuses a URL

- By default, `fetch_web_content` blocks localhost, private LAN, link-local, multicast, and reserved IP ranges, including redirects to those ranges.
- Set `ALLOW_PRIVATE_WEB_FETCH=true` only if you intentionally want the model to fetch local or LAN URLs.

### `/expert open` cannot find an archived report

- Confirm `EXPERT_ARCHIVE_DIR` points at the directory where archived session folders live.
- Confirm `config/expert_sessions.json` is bind-mounted or persisted with the archive folder if running in Docker.
- If the error references `/root/expert`, update `.env` to `EXPERT_ARCHIVE_DIR=data/expert` for future archives.
- Old archives are not automatically migrated. If you need an old report, copy its archive folder into the configured `EXPERT_ARCHIVE_DIR` and update/recreate the matching index entry.

### Docker created folders instead of files

You likely started Docker before creating the bind-mounted files. Stop the container, replace those directories with files, then restart.

### Voice replies fail

- Confirm `ffmpeg` is installed locally, or available in the container.
- Confirm `TTS_URL` is reachable.
- Confirm `OPEN_WEBUI_KEY` is valid if your TTS/STT service requires it.

### Calendar or Nest fails

- Re-run `python scripts/generate_google_token.py`.
- Confirm the expected token files exist under `secrets/google/`.
- Make sure your OAuth app is configured correctly in Google Cloud.
