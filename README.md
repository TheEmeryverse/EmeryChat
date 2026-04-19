# EmeryChat
A telegram wrapper for AI models with advanced tool use.

Many assistant AI agent wrappers require massive context, hugely powerful cloud models, and massive token usage. EmeryChat aims to change that, trading the "self-learning" and "It's Alive!!" hype for real, solid, consistent performance that is achievable on normal consumer hardware. No ridiculously complex setup, no CLI, and no cloud API costs. Run EmeryChat for FREE, on your hardware.

EmeryChat operates under a simple idea. Speed doesn't matter, much. Emerychat uses Telegram as the chat interface, and much of the core functionality comes from the scheduled jobs that allow the model to operate overnight on complex tasks. If you are chatting during the day, speed will be quick enough and the same as if you are texting a real person.

All models have been tested using ONLY the CPU for inference, as part of the overarching goal of making EmeryChat usable on consumer hardware.

Tested with:
- Gemini 3 Flash (Cloud-based API), A
- Gemma4:26b MoE (64k context, thinking ON), A+
- Qwen3.6:35b MoE (64k context, thinking OFF), A
- Gemma4:e4b (64k context, thinking ON), B+
- Qwen3.5:9b (64k context, thinking ON), B

All models have been tested on a 32GB DDR4 RAM, AMD 5950x CPU, and Intel Arc B580 GPU (12GB VRAM) system. Smaller models have been tested on a base M4 Mac Mini (16GB Unified Memory) utlizing the MLX framework.

Tools Built (So Far...)

- Live Weather Data with NOAA
- Google Calendar Integration
- Text to Speech, allowing Emery to send you voice messages.
- Voice message transcription using Open WebUI's STT, so you can send voice messages.
- RSS News Feeds from any RSS feed you want.
- NASA's Image of the Day
- Today In History
- Web Search with SearXNG
- System Stats, such as CPU and RAM usage.
- Seerr integration, searching and requesting movies and TV shows on your behalf.
- Image generation, using Gemini or another provider of your choice.
- Vision capable, either through using a multimodal model or a secondary vision model, such as Gemm4:e2b for lightning speed.
