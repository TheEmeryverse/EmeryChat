</head>
<body>
  <div class="wrapper">
    <header class="hero">
      <h1>EmeryChat</h1>
      <p class="tagline">A Telegram wrapper for AI models with advanced tool use.</p>
      <p class="lead">Many assistant AI agent wrappers require massive context, hugely powerful cloud models, and massive token usage. EmeryChat aims to change that, trading the "self-learning" and "It's Alive!!" hype for real, solid, consistent performance that is achievable on normal consumer hardware. No ridiculously complex setup, no CLI, and no cloud API costs. Run EmeryChat for FREE, on your hardware.</p>
    </header>

    <section class="philosophy">
      <h2>How It Works</h2>
      <p>EmeryChat operates under a simple idea. <span class="highlight">Speed doesn't matter, much.</span></p>
      <p>It uses Telegram as the chat interface, and much of the core functionality comes from scheduled jobs that allow the model to operate overnight on complex tasks. If you are chatting during the day, speed will be quick enough and the same as if you are texting a real person.</p>
      <p>All models have been tested using <strong>ONLY the CPU</strong> for inference, as part of the overarching goal of making EmeryChat usable on consumer hardware.</p>
    </section>

    <section class="benchmarks">
      <h2>Tested Models & Performance</h2>
      <table class="bench-table">
        <thead>
          <tr><th>Model</th><th>Context / Notes</th><th>Grade</th></tr>
        </thead>
        <tbody>
          <tr><td>Gemini 3 Flash (Cloud-based API)</td><td>—</td><td><span class="grade A">A</span></td></tr>
          <tr><td>Gemma4:26b MoE</td><td>64k context, thinking ON</td><td><span class="grade A">A+</span></td></tr>
          <tr><td>Qwen3.6:35b MoE</td><td>64k context, thinking OFF</td><td><span class="grade A">A</span></td></tr>
          <tr><td>Gemma4:e4b</td><td>64k context, thinking ON</td><td><span class="grade B">B+</span></td></tr>
          <tr><td>Qwen3.5:9b</td><td>64k context, thinking ON</td><td><span class="grade B">B</span></td></tr>
        </tbody>
      </table>
    </section>

    <section class="hardware">
      <h2>Testing Hardware</h2>
      <p>All models have been tested on:</p>
      <div class="specs">
        <span>32GB DDR4 RAM</span>
        <span>AMD 5950x CPU</span>
        <span>Intel Arc B580 GPU (12GB VRAM)</span>
      </div>
      <p style="margin-top:0.8rem;">Smaller models have been tested on a base M4 Mac Mini (16GB Unified Memory) utilizing the MLX framework.</p>
    </section>

    <section class="tools">
      <h2>Tools Built (So Far...)</h2>
      <div class="tools-grid">
        <div class="tool-card"><span class="tool-title">🌤 Live Weather Data</span><p class="tool-desc">With NOAA integration</p></div>
        <div class="tool-card"><span class="tool-title">📅 Google Calendar</span><p class="tool-desc">Full scheduling integration</p></div>
        <div class="tool-card"><span class="tool-title">🎙 Text to Speech</span><p class="tool-desc">Allows Emery to send voice messages</p></div>
        <div class="tool-card"><span class="tool-title">🎧 Voice Transcription</span><p class="tool-desc">Using Open WebUI's STT for inbound messages</p></div>
        <div class="tool-card"><span class="tool-title">📡 RSS News Feeds</span><p class="tool-desc">Pull from any custom RSS source</p></div>
        <div class="tool-card"><span class="tool-title">🚀 NASA Image of the Day</span><p class="tool-desc">Daily space imagery & explanations</p></div>
        <div class="tool-card"><span class="tool-title">📜 Today In History</span><p class="tool-desc">Historical context on command</p></div>
        <div class="tool-card"><span class="tool-title">🔍 Web Search</span><p class="tool-desc">Powered by SearXNG</p></div>
        <div class="tool-card"><span class="tool-title">💻 System Stats</span><p class="tool-desc">Real-time CPU & RAM monitoring</p></div>
        <div class="tool-card"><span class="tool-title">🎬 Seerr Integration</span><p class="tool-desc">Search & request movies/TV shows</p></div>
        <div class="tool-card"><span class="tool-title">🖼 Image Generation</span><p class="tool-desc">Using Gemini or another provider</p></div>
        <div class="tool-card"><span class="tool-title">👁 Vision Capable</span><p class="tool-desc">Multimodal models or fast secondary (e.g., Gemma4:e2b)</p></div>
      </div>
    </section>

    <footer>
      <p>EmeryChat © 2024 — Free. Open. Runs locally.</p>
    </footer>
  </div>
</body>
</html>
