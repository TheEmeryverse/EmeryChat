# 🛡️ EmeryChat

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey.svg)]()
[![Docker Support](https://img.shields.io/badge/docker-ready-cyan.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()

A powerful Telegram bot wrapper for local and cloud AI models featuring advanced tool usage, local agentic memory, and smart integrations.

Many AI agent wrappers require massive context, cloud-only models, and heavy token billing. **EmeryChat** trades the hype for consistent, high-utility performance designed to run **locally on consumer hardware for free** (with optional cloud fallbacks). Run EmeryChat on your local CPU or GPU, access private memory, command home devices, search the web, check cameras, and manage media — all from Telegram.

---

## 📖 Table of Contents
1. [How It Works](#-how-it-works)
2. [Model Performance Matrix](#-model-performance-matrix)
3. [Hardware Requirements](#-hardware-requirements)
4. [Quickstart Setup Guide](#%EF%B8%8F-quickstart-setup-guide)
   - [Step 1: Telegram Bot Creation](#step-1-telegram-bot-creation)
   - [Step 2: Local & Cloud Model Setup (Ollama / Open WebUI)](#step-2-local--cloud-model-setup-ollama--open-webui)
   - [Step 3: Environment Configuration](#step-3-environment-configuration)
   - [Step 4: Google Service Authentication (Calendar & Nest)](#step-4-google-service-authentication-calendar--nest)
   - [Step 5: Run the Application](#step-5-run-the-application)
5. [🧠 Persistent Memory System](#-persistent-memory-system)
6. [🛠️ Tool Library & Configuration](#%EF%B8%8F-tool-library--configuration)
7. [📅 Task Scheduling & Automated Briefings](#-task-scheduling--automated-briefings)
8. [⚙️ Environment Variables Reference](#%EF%B8%8F-environment-variables-reference)

---

## ⚡ How It Works

EmeryChat operates under a simple philosophy: **consistency and utility over instant speed.**
* **Telegram Interface:** Uses Telegram as a natural chat interface.
* **Scheduled Operations:** Heavy cognitive tasks, research briefings, and long-term planning are handled through background scheduled jobs (e.g. running overnight while you sleep).
* **CPU-Friendly Inference:** Fully optimized to run on local CPU threads utilizing context-pruning algorithms, local text models, and secondary fast models for background tasks.
* **Multi-Model Pipeline:** Uses a primary text-generating model (e.g., Qwen 35B or Gemma 26B) and offloads vision/image analysis, background memory consolidation, web summarization, and custom lightweight sub-tasks to a secondary fast "coprocessor" model (e.g. Gemma 4B/9B) to save resources.

---

## 📊 Model Performance Matrix

EmeryChat supports any model that compiles with Ollama or standard Open WebUI / OpenAI-compatible completion formats. Below are models tested and verified:

| Model | Context size | Grade | Strengths & Weaknesses |
| :--- | :--- | :---: | :--- |
| **Gemini 3.5 Flash** | Cloud API | **A** | Excellent quality, lightning-fast, multimodal. Overkill but flawless. |
| **Qwen 3.6:35b MoE** | 64k, Thinking OFF | **A** | Instant responses, incredible tool calling. Outstanding research reports. Tends to call tools proactively. |
| **Gemma 4:26b MoE** | 64k, Thinking ON | **A-** | Intelligent tool use. Leverages thinking logic. Can occasionally offer shorter responses unless prompted. |
| **Gemma 4:e4b** | 64k, Thinking ON | **B+** | Highly efficient, runs on low-end hardware. Excellent secondary/coprocessor model. |
| **Qwen 3.5:9b** | 64k, Thinking ON | **B** | Solid budget entry. Good for basic tasks, but can hallucinate details under high context. |

---

## 💻 Hardware Requirements

EmeryChat is designed to run locally:
* **GPU Dedicated Setup (Tested):** AMD 5950x CPU, 32GB DDR4 RAM, Intel Arc B580 GPU (12GB VRAM) running Ollama.
* **Base Setup (Tested):** M4 Mac Mini (16GB Unified Memory) utilizing the Apple MLX framework or Ollama.
* **CPU-Only Setup:** Can run on multi-core consumer CPUs; response generation takes 10–30s during active chat, making it ideal for background job execution.

---

## 🛠️ Quickstart Setup Guide

### Step 1: Telegram Bot Creation
1. Find **[@BotFather](https://t.me/botfather)** on Telegram.
2. Send `/newbot` and follow the prompts to create your bot.
3. Save the **HTTP API Token** (you will paste this as `TELEGRAM_TOKEN` in your `.env` file).
4. Send a greeting to your new bot on Telegram. The bot will automatically capture your `chat_id` on the first message.

---

### Step 2: Local & Cloud Model Setup (Ollama / Open WebUI)
EmeryChat requires an Ollama server (or equivalent OpenAI-style API) for text generation, plus an Open WebUI API key for voice features (STT).

1. **Install Ollama:** Follow the guide at [ollama.com](https://ollama.com).
2. **Download Models:**
   ```bash
   ollama pull qwen3.6:35b-a3b
   ollama pull gemma4:e4b
   ```
3. **Verify Ollama URL:** By default, Ollama serves on `http://localhost:11434`. If running in Docker, you may need to point this to `http://host.docker.internal:11434`.

---

### Step 3: Environment Configuration
1. Clone this repository:
   ```bash
   git clone https://github.com/TheEmeryverse/EmeryChat.git
   cd EmeryChat
   ```
2. Copy `example.env` to `.env`:
   ```bash
   cp example.env .env
   ```
3. Open `.env` and fill in the required variables (see [Environment Variables Reference](#%EF%B8%8F-environment-variables-reference) below).

---

### Step 4: Google Service Authentication (Calendar & Nest)
Google Calendar and Nest Thermostat integrations require OAuth2 credentials.

1. **Create Google Cloud Project:**
   - Visit the [Google Cloud Console](https://console.cloud.google.com/).
   - Create a project and enable the **Google Calendar API** and the **Smart Device Management API** (Nest).
   - Go to **APIs & Services > OAuth consent screen**. Set publishing status to **In Production** (otherwise tokens expire every 7 days).
   - Go to **Credentials**, click **Create Credentials > OAuth client ID**, and select **Desktop Application**.
   - Download the client secrets JSON.
2. **Configure Credentials Paths:**
   - Save the downloaded JSON as `credentials.json` in the root directory (for Calendar).
   - If using Nest, download another copy or link it, saving it as `nest_credentials.json`.
3. **Run the Helper Script:**
   Generate authorization tokens by running the interactive helper script:
   ```bash
   python generate_google_token.py
   ```
   Select option `1` to authorize **Google Calendar** (generates `token.json`).
   Select option `2` to authorize **Google Nest** (generates `nest_token.json`).
   Follow the on-screen URLs, log in with Google, authorize the requested permissions, and copy-paste the redirected authorization code back into the terminal.

---

### Step 5: Run the Application

You can run EmeryChat directly using Python or inside a Docker container.

#### Option A: Running with Virtual Environment (Local)
1. Ensure Python 3.10+ and `ffmpeg` are installed on your host system:
   ```bash
   # macOS
   brew install ffmpeg
   ```
2. Set up the virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r Dockerfile  # Or install individual packages listed below
   ```
3. Install the dependencies:
   ```bash
   pip install "python-telegram-bot[job-queue]" httpx requests Pillow feedparser psutil pytz tghtml markdown python-dotenv google-api-python-client google-auth-httplib2 google-auth-oauthlib
   ```
4. Start the bot:
   ```bash
   python main.py
   ```

#### Option B: Running with Docker Compose (Recommended)
1. **Initialize persistent files** on the host. Docker requires bind-mounted files to exist on the host before starting the container, otherwise it creates directories:
   ```bash
   touch memory.md token.json nest_token.json credentials.json nest_credentials.json
   ```
2. **Build and start the container** in detached mode:
   ```bash
   docker compose up --build -d
   ```
3. **Monitor logs:**
   ```bash
   docker compose logs -f
   ```
4. **Stop the container:**
   ```bash
   docker compose down
   ```

---

## 🧠 Persistent Memory System

EmeryChat includes an advanced local memory module that stores user facts and preferences to a local file (`memory.md`), eliminating token creep while maintaining long-term awareness.

```mermaid
flowchart TD
    A["User sends Message"] --> B["Memory Engine filters memory.md by keywords<br>(Only loads relevant facts into system prompt)"]
    B --> C["Primary LLM processes query & calls tools<br>(e.g. calls save_user_memory('Moved to NYC'))"]
    C --> D["New facts appended to 'Raw Memory Intake'"]
    D --> E["Background Coprocessor Model consolidates memory.md<br>(Deduplicates, resolves contradictions, clears intake)"]
```

* **Staging Area:** When the bot learns a new detail, it calls the `save_user_memory` tool. This appends the fact to a `## Raw Memory Intake` header in `memory.md`.
* **Background Consolidation:** In the background, EmeryChat spawns a separate non-blocking asynchronous task using the secondary coprocessor model (`VISION_MODEL_ID`) to parse `memory.md`, deduplicate logs, resolve contradictions, categorize information into preferences/logs, and clear the raw staging area.
* **Memory Management Commands:**
  * Send `/clear` to clear current chat thread context history.
  * Send `/wipe` to wipe the persistent `memory.md` file back to the baseline config template (reads user configurations from `.env`).

---

## 🛠️ Tool Library & Configuration

Below is a listing of the tools available in EmeryChat. You can toggle each tool in your `.env` file.

<details>
<summary>📂 View Google & Home Integration Tools</summary>

### Google Calendar (`get_calendar_events`)
* **Status Toggle:** `ENABLE_CALENDAR=true`
* **Description:** Queries your configured Google Calendars, formats the daily agenda chronologically (combining all-day and multi-day events), and displays it.
* **Dependencies:** Requires `credentials.json` and a generated `token.json` via option 1 of `generate_google_token.py`.
* **Env Config:**
  ```env
  GOOGLE_CALENDAR_IDS=primary,your_other_calendar_id@gmail.com
  GOOGLE_TOKEN_PATH=token.json
  ```

### Nest Thermostat Controls
* **Status Toggle:** `ENABLE_NEST=true`
* **Description:** Manages Google Nest Smart Thermostats using the Device Access API (SDM).
* **Dependencies:** Requires `nest_credentials.json` and `nest_token.json` via option 2 of `generate_google_token.py`.
* **Available Functions:**
  * `get_nest_thermostats()`: Returns status, ambient temp, target temperature setpoints, humidity, HVAC running status, and supported modes for all thermostats linked to the account.
  * `set_nest_thermostat_mode(device_id, mode)`: Sets thermostat mode (`HEAT`, `COOL`, `HEATCOOL`, `OFF`).
  * `set_nest_thermostat_temperature(device_id, temp_celsius, heat_temp_celsius, cool_temp_celsius)`: Sets the target temperature (automatically parses input Celsius/Fahrenheit and executes commands based on the current mode).
* **Env Config:**
  ```env
  NEST_PROJECT_ID=YOUR_NEST_DEVICE_ACCESS_PROJECT_ID  # UUID from Device Access Console
  NEST_TOKEN_PATH=nest_token.json
  ```

### Reolink Security Cameras
* **Status Toggle:** `ENABLE_REOLINK=true`
* **Description:** Connects to local Reolink NVR/IP security cameras to fetch live streams and run visual threat checks.
* **Available Functions:**
  * `get_reolink_snapshot(camera_name)`: Grabs a JPEG snapshot from the camera, uploads it to the user via Telegram, runs a dedicated threat audit using the coprocessor model, and sends structural environmental updates to the bot's background memory context.
  * `get_available_cameras()`: Lists online cameras.
* **Active Security Polling:** If `ENABLE_REOLINK_POLLING=true`, the bot starts a background NVR listener. If the NVR registers an AI "person detected" state, the bot wakes up, notifies the user, fetches a snapshot, runs a vision check, and updates the chat thread with security logs.
* **Successive Alert Threading:** If `ENABLE_REOLINK_THREADING=true`, successive alerts on the same camera within a configurable window (`REOLINK_THREAD_WINDOW_MINUTES`, default: 10 minutes) reply to the first alert's message. This prevents alert flooding in the main Telegram chat.
* **Env Config:**
  ```env
  REOLINK_HOST=192.168.1.100  # Local IP/Host of NVR
  REOLINK_USER=admin
  REOLINK_PASSWORD=your_nvr_password
  REOLINK_CAMERAS=frontdoor:0,backyard:1  # Map of friendly_name:nvr_channel
  REOLINK_CAMERA_DESCRIPTIONS=frontdoor:doorbell_camera_facing_porch... # Helps vision model understand context
  ENABLE_REOLINK_POLLING=true  # Background person detection trigger
  ```
</details>

<details>
<summary>📂 View Research & Information Tools</summary>

### Web Search (`web_search`)
* **Status Toggle:** `ENABLE_SEARCH=true`
* **Description:** Performs a live web search using SearXNG. It aggregates the top 5 results (title, snippet, and URL) for research context.
* **Env Config:**
  ```env
  SEARXNG_URL=http://localhost:8080/search
  ```

### Web Scraping (`fetch_web_content`)
* **Status Toggle:** `ENABLE_WEB_SCRAPING=true`
* **Description:** Fetches raw HTML from a target URL, strips irrelevant tags (scripts, CSS, navigations, footers), formats list tags and headers, and returns the parsed article text. **Coprocessor-Optimized:** If the parsed text exceeds 1,500 characters, it is automatically summarized by the fast coprocessor model to prevent context bloat on the main model.
* **No external configuration required.**

### RSS News Feed (`get_news_headlines`)
* **Status Toggle:** `ENABLE_NEWS=true`
* **Description:** Reads and consolidates headlines from customized RSS feeds.
* **Env Config:**
  ```env
  NEWS_FEEDS="REUTERS|url, FOX|url, TECH|url"  # Pipe-delimited name and URL pairs, comma-separated
  ```

### Today In History (`get_today_in_history`)
* **Status Toggle:** `ENABLE_HISTORY=true`
* **Description:** Fetch historical events, births, and deaths for the current day from `dayinhistory.dev`.
* **No external configuration required.**

### NASA Image of the Day (`get_nasa_apod`)
* **Status Toggle:** `ENABLE_NASA=true`
* **Description:** Fetches NASA's Astronomy Picture of the Day (APOD) title, explanation, and raw image link.
* **Env Config:**
  ```env
  NASA_API_KEY=YOUR_NASA_API_KEY
  ```
</details>

<details>
<summary>📂 View Media, Voice & Generation Tools</summary>

### Kokoro Text-To-Speech (`speak_message`)
* **Status Toggle:** `ENABLE_VOICE=true`
* **Description:** Converts text generated by the bot into a voice memo. It strips markdown artifacts, queries Kokoro TTS, converts the output to an Ogg/Opus stream using local `ffmpeg`, and sends it to Telegram.
* **Env Config:**
  ```env
  TTS_URL=http://localhost:8880/v1/audio/speech
  TTS_VOICE=af_heart  # Kokoro voice name
  ```

### Speech-to-Text Transcription (STT)
* **Status Toggle:** Active automatically when the user sends a voice memo.
* **Description:** The bot intercepts voice messages, downloads them, queries your transcription service (e.g. Open WebUI STT), and feeds the transcription to the model.
* **Env Config:**
  ```env
  STT_URL=http://localhost:3000/api/v1/audio/transcriptions
  OPEN_WEBUI_KEY=YOUR_OPEN_WEBUI_KEY
  ```

### Image Generation (`generate_image`)
* **Status Toggle:** `ENABLE_IMAGEGEN=true`
* **Description:** Generates images from a text prompt via the Gemini Image API. It will automatically deliver the generated photo directly to your Telegram chat.
* **Env Config:**
  ```env
  GEMINI_API_KEY=YOUR_GEMINI_API_KEY
  IMAGE_MODEL=gemini-3.1-flash-image-preview
  ```

### Multimodal Vision Coprocessor & Delegation
* **Status Toggle:** Active automatically for photos, memory consolidation, and dynamic delegation.
* **Description:** Integrates a secondary vision-capable coprocessor model (`VISION_MODEL_ID`) to handle visual checks, memory staging, and offload processing from the main model:
  - **Image Analysis**: Processes user-uploaded photos and Reolink NVR camera feeds.
  - **Memory Consolidation**: Deduplicates and organizes `memory.md` in the background.
  - **Automatic Summarization**: Summarizes web content from `fetch_web_content` when it exceeds 1,500 characters.
  - **Dynamic Delegation (`delegate_to_coprocessor`)**: A core tool allowing the main model to explicitly offload parsing, formatting, or extraction tasks. Enforced via system prompt rules to prevent model confidence-bias/over-prefilling.
* **Env Config:**
  ```env
  VISION_MODEL_ID=gemma4:e4b
  VISION_OLLAMA_URL=http://localhost:11434/api/chat
  OLLAMA_VISION_NUM_CTX=65536
  ```

### Seerr/Overseerr Media Requests
* **Status Toggle:** `ENABLE_SEERR=true`
* **Description:** Connects to Overseerr/Seerr to request media.
* **Available Functions:**
  * `overseer_search_movie(query)`: Search movies.
  * `overseer_request_movie(tmdb_id)`: Request a movie.
  * `overseer_search_tv(query)`: Search TV shows.
  * `overseer_request_tv_season(tmdb_id, season_number)`: Request TV show seasons.
* **Env Config:**
  ```env
  OVERSEER_URL=http://localhost:5055/api/v1
  OVERSEER_KEY=YOUR_OVERSEER_KEY
  OVERSEER_USER_ID=1
  ```

### System Stats (`get_system_stats`)
* **Status Toggle:** `ENABLE_SYSTEM_STATS=true`
* **Description:** Inspects current host CPU and memory utilization using `psutil`.
* **No external configuration required.**
</details>

---

## 📅 Task Scheduling & Automated Briefings

EmeryChat features a dynamic background task scheduler. Instead of relying purely on hardcoded scripts, users can instruct the bot to create, list, and cancel schedules using natural language. 

Custom jobs are saved in a local, git-ignored `custom_jobs.json` file. The bot reloads and registers these triggers in memory on startup.

### 🛠️ Available Scheduling Commands
You can ask the bot to schedule tasks with triggers such as:
1. **Daily repeating schedules**: *"Remind me to check the news every day at 8:30 AM"* (triggers at a specific time daily).
2. **Repeating interval schedules**: *"Check the front camera every 30 minutes"* (triggers repeatedly on a delay).
3. **One-off schedules / reminders**: *"Remind me to check the oven in 15 minutes"* or *"Remind me on 2026-05-26 at 15:30:00 to run backups"* (triggers once and automatically deletes itself from storage after running).

Emery utilizes three specific tool call routines behind the scenes to manage this:
* `add_scheduled_job(schedule_type, schedule_value, prompt, description)`: Registers and schedules a task.
* `list_scheduled_jobs()`: Returns all active user-scheduled jobs.
* `remove_scheduled_job(job_id)`: Deletes and cancels a job trigger.

### 📋 Pre-configured Daily Briefings
For new users, the scheduler starts empty (blank slate). To reproduce the bot's default daily briefings, you can ask Emery to schedule them. The templates for these jobs are:

| Trigger (Daily) | Description | Prompt / Action |
| :---: | :--- | :--- |
| **03:00 AM** | **Morning Briefing** | *"Morning news intel from get_news_headlines. List all of the stories first, and hone in on the most important one at the end with a deep dive using web_search and fetch_web_content. Put all of it in a voice memo, and then also put everything in your text response. Do not include any sports news, and assess bias of any sources."* |
| **03:05 AM** | **Today's Weather** | *"Look up weather with the get_NOAA_weather tool and give clothing recommendations while keeping in mind the User Bio."* |
| **03:10 AM** | **Daily Planner** | *"Check User's calendar with get_calendar_events for any events the User has today and list them chronologically."* |
| **09:00 PM** | **Today In Space** | *"Use get_nasa_apod. Provide title, explanation, and MUST provide image URL link."* |
| **09:05 PM** | **Today In History** | *"Use get_today_in_history. Provide the returned items in a presentable list, then focus on one of the people and do research with web_search and fetch_web_content and give a small report on them at the end of your response."* |

---

## ⚙️ Environment Variables Reference

Below is a detailed list of the configurations available in your `.env` file:

| Variable | Default Value | Description |
| :--- | :---: | :--- |
| `TELEGRAM_TOKEN` | *Required* | API Key generated via @BotFather on Telegram. |
| `MODEL_NAME` | `Emery` | Name the bot addresses itself as. |
| `MODEL_ID` | `qwen3.6:35b-a3b` | Main model ID in Ollama. |
| `VISION_MODEL_ID` | `gemma4:e4b` | Coprocessor vision and processing model. |
| `OLLAMA_NUM_CTX` | `65536` | Context size of the main text model. |
| `OLLAMA_VISION_NUM_CTX` | `65536` | Context size of the vision coprocessor model. |
| `OLLAMA_URL` | `http://localhost:11434/api/chat` | Ollama connection endpoint for main model. |
| `VISION_OLLAMA_URL` | `http://localhost:11434/api/chat` | Ollama connection endpoint for coprocessor model. |
| `ENABLE_THINKING` | `true` | Show thinking reasoning blocks in Telegram chats. |
| `ENABLE_MEMORY` | `true` | Enables persistent local long-term memory. |
| `MEMORY_FILE_PATH` | `memory.md` | Path to save persistent memories. |
| `MEMORY_THRESHOLD` | `4000` | Token threshold. Memory is filtered by keyword when context exceeds this value. |
| `OPEN_WEBUI_KEY` | `blank` | API Key for Open WebUI access (for voice features). |
| `OPEN_WEBUI_URL` | `http://localhost:3000/api/v1/chat/...` | Connection endpoint for Open WebUI. |
| `STT_URL` | `http://localhost:3000/api/v1/audio/...` | Speech-to-Text translation API endpoint. |
| `USER_NAME` | `User` | Name of the user (used in bios and greetings). |
| `USER_BIRTHDAY` | `January 1, 2000` | User birthday (monitored for dynamic notifications). |
| `USER_LOCATION` | `New York City, NY` | User city location (for default coordinate and weather maths). |
| `USER_TIMEZONE` | `America/New_York` | User timezone identifier (for schedule parsing and date math). |
| `USER_PROFESSION` | `AI Enthusiast` | User job details (helps customize recommendations). |
| `ENABLE_CALENDAR` | `false` | Enable Google Calendar tool. |
| `GOOGLE_CALENDAR_IDS` | `primary` | Comma-separated list of calendars to parse. |
| `ENABLE_NEST` | `false` | Enable Google Nest Thermostat tools. |
| `NEST_PROJECT_ID` | *Required if Nest active* | Device Access UUID from Nest console. |
| `ENABLE_WEATHER` | `false` | Enable NOAA Weather integration. |
| `NOAA_LAT` / `NOAA_LONG` | `40.7128` / `-74.0060` | Exact latitude and longitude for weather reports. |
| `NOAA_EMAIL` | `example@example.com` | Email user-agent identifier required by NOAA API rules. |
| `ENABLE_NEWS` | `false` | Enable RSS feed parser tool. |
| `NEWS_FEEDS` | *Reuters, Fox, Tech* | Custom feeds string format: `"NAME\|URL, NAME\|URL"`. |
| `ENABLE_NASA` | `false` | Enable NASA APOD tools. |
| `NASA_API_KEY` | `DEMO_KEY` | Developer API key from NASA. |
| `ENABLE_VOICE` | `false` | Enable outbound voice message (Kokoro TTS). |
| `TTS_URL` | `http://localhost:8880/v1/audio/...` | Connection endpoint for Kokoro server. |
| `TTS_VOICE` | `af_heart` | Kokoro TTS voice model profile. |
| `ENABLE_IMAGEGEN` | `false` | Enable Gemini image generation. |
| `GEMINI_API_KEY` | *Required if ImageGen active* | Developer API key from Google. |
| `ENABLE_SEARCH` | `false` | Enable web search query tool. |
| `SEARXNG_URL` | `http://localhost:8080/search` | SearXNG instance endpoint. |
| `ENABLE_WEB_SCRAPING`| `false` | Enable website URL content reading tool. |
| `ENABLE_SYSTEM_STATS` | `false` | Enable CPU and Virtual memory reading tool. |
| `ENABLE_REOLINK` | `false` | Enable Reolink CCTV snapshots and queries. |
| `ENABLE_REOLINK_POLLING`| `false` | Enables active background loop checking NVR for AI alerts. |
| `ENABLE_REOLINK_THREADING`| `true` | Enables grouping successive camera alerts into a Telegram reply thread. |
| `REOLINK_THREAD_WINDOW_MINUTES`| `10` | Time window (in minutes) to group subsequent alerts under the first alert of the thread. |
| `TOOL_LOOP` | `15` | Maximum back-and-forth tool call loops in one turn. |
| `ENABLE_SCHEDULER` | `true` | Enables/disables the custom background scheduler. |
| `JOBS_FILE_PATH` | `custom_jobs.json` | Local filepath where custom schedules are persisted. |
