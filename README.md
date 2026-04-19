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
      <tr>
        <th>Model</th>
        <th>Context/Logic</th>
        <th>Grade</th>
        <th>Explanation</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>Gemini 3 Flash</td>
        <td>Cloud API</td>
        <td><span class="grade A">A</span></td>
        <td>You get what you pay for. In this case excellent quality and instant responses. Overkill for EmeryChat.</td>
      </tr>
      <tr>
        <td>Gemma4:26b MoE</td>
        <td>64k, Thinking ON</td>
        <td><span class="grade A-">A-</span></td>
        <td>Quick responses, intelligent tool usage. Tends to be a bit "lazy", favoring short responses even when asked to go in depth.</td>
      </tr>
      <tr>
        <td>Qwen3.6:35b MoE</td>
        <td>64k, Thinking OFF</td>
        <td><span class="grade A">A</span></td>
        <td>Quick responses, excellent tool calling. Will easily generate long breakdowns and research reports. Tends to be to "pleasing", and may call tools a bit proactively without direct prompting.</td>
      </tr>
      <tr>
        <td>Gemma4:e4b</td>
        <td>64k, Thinking ON</td>
        <td><span class="grade B">B+</span></td>
        <td>Extremely efficient; runs on almost any hardware but occasionally misses nuanced logic. Very strong tool usage.</td>
      </tr>
      <tr>
        <td>Qwen3.5:9b</td>
        <td>64k, Thinking ON</td>
        <td><span class="grade B">B</span></td>
        <td>Solid entry-level model; great for basic tasks but has a tendency to deliver long responses that lack depth and contain hallucinated or incorrect information.</td>
      </tr>
    </tbody>
  </table>
</section>
    <section class="hardware">
      <h2>Testing Hardware</h2>
      <p>All models have been tested on:</p>
      <div class="specs">
        <span>32GB DDR4 RAM,</span>
        <span>AMD 5950x CPU,</span>
        <span>Intel Arc B580 GPU (12GB VRAM)</span>
      </div>
      <p style="margin-top:0.8rem;">Smaller models have been tested on a base M4 Mac Mini (16GB Unified Memory) utilizing the MLX framework.</p>
    </section>
    <section class="tools">
      <h2>Tools Built (So Far...)</h2>
      <table class="bench-table">
        <thead>
          <tr><th>Feature</th><th>Description / Integration</th></tr>
        </thead>
        <tbody>
          <tr><td>🌤 Live Weather</td><td>Real-time data with NOAA integration</td></tr>
          <tr><td>📅 Google Calendar</td><td>Emery can view your Google Calendar</td></tr>
          <tr><td>🎙 Text to Speech</td><td>Allows Emery to send outbound voice messages</td></tr>
          <tr><td>🎧 Transcription</td><td>Using Open WebUI's STT for your voice messages</td></tr>
          <tr><td>📡 RSS News</td><td>Custom feeds from any RSS source</td></tr>
          <tr><td>🚀 NASA Image</td><td>Daily space imagery and explanations</td></tr>
          <tr><td>📜 History</td><td>"Today In History" historical content</td></tr>
          <tr><td>🔍 Web Search</td><td>Integrated search powered by SearXNG</td></tr>
          <tr><td>🔍 URL Fetching</td><td>Allows you to send links, and for Emery do to deep research</td></tr>
          <tr><td>💻 System Stats</td><td>Real-time CPU and RAM monitoring</td></tr>
          <tr><td>🎬 Seerr / Media</td><td>Request movies and TV shows via Seerr</td></tr>
          <tr><td>🖼 Image Gen</td><td>Generate images via Gemini or custom providers</td></tr>
          <tr><td>👁 Vision</td><td>Multimodal support or fast secondary vision models</td></tr>
        </tbody>
      </table>
    </section>
  </div>
</body>
