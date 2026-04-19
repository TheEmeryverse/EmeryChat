<!DOCTYPE html>
    
    header { text-align: center; margin-bottom: 2rem; }
    h1 {
      font-size: clamp(2rem, 5vw, 3rem);
      font-weight: 800;
      background: linear-gradient(135deg, var(--accent), #a78bfa);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      margin-bottom: 0.4rem;
    }
    .tagline { font-size: 1.2rem; color: var(--muted); margin-bottom: 1.5rem; }
    .lead { max-width: 640px; margin: 0 auto 2.5rem; text-align: center; }

    section {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1.5rem;
      margin-bottom: 1.8rem;
      box-shadow: 0 6px 12px rgba(0,0,0,0.25);
    }
    h2 {
      font-size: 1.4rem;
      color: var(--accent);
      margin-bottom: 1rem;
      padding-bottom: 0.5rem;
      border-bottom: 1px dashed var(--border);
    }
    p, ul { margin-bottom: 1rem; }
    
    /* Benchmarks Table */
    .bench-table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
    .bench-table th, .bench-table td { padding: 0.7rem 0.8rem; text-align: left; border-bottom: 1px solid var(--border); }
    .bench-table th { color: var(--muted); font-weight: 600; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.5px; }
    .grade { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 8px; font-weight: 700; font-size: 0.85rem; }
    .grade.A { background: rgba(34,197,94,0.2); color: var(--grade-a); }
    .grade.B { background: rgba(245,158,11,0.2); color: var(--grade-b); }

    /* Tools Grid */
    .tools-grid { display: grid; gap: 0.9rem; grid-template-columns: 1fr; }
    @media (min-width: 600px) { .tools-grid { grid-template-columns: repeat(2, 1fr); } }
    .tool-card {
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--border);
      padding: 0.9rem;
      border-radius: 10px;
      transition: transform 0.2s, border-color 0.2s;
    }
    .tool-card:hover { transform: translateY(-2px); border-color: var(--accent); }
    .tool-title { font-weight: 600; color: var(--accent); margin-bottom: 0.3rem; display: block; }
    .tool-desc { font-size: 0.95rem; color: var(--muted); margin: 0; }

    /* Utilities */
    .highlight { color: #34d399; font-weight: 500; }
    .specs { display: flex; flex-wrap: wrap; gap: 1rem; font-size: 0.95rem; color: var(--muted); }
    .specs span { background: rgba(255,255,255,0.05); padding: 0.4rem 0.7rem; border-radius: 8px; }
    footer { text-align: center; font-size: 0.9rem; color: var(--muted); margin-top: 2rem; opacity: 0.7; }

    /* Smooth scroll & base */
    html { scroll-behavior: smooth; }
  </style>
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
