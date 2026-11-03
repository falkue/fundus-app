# Installing Fundus

The detailed, step-by-step guide – written so that an AI coding assistant can follow it, with checks and
troubleshooting for Linux, macOS and Windows – is **[AGENTS.md](AGENTS.md)**. The easiest way: open the repository in
your assistant (Claude Code, Codex, Gemini CLI, Cursor …) and ask *"Install Fundus by following AGENTS.md."*

> Linux is tested; **macOS and Windows are untested so far**. If a step fails, please open an issue with the step and
> the error message (no archive contents, no personal data).

## In short

1. **Prerequisites:** Python 3.12 (3.11 works) and git. Optional: a language-model server (e.g. Ollama), ffmpeg,
   poppler.
2. **Install** into a virtual environment:
   ```bash
   git clone https://github.com/falkue/fundus-app.git fundus && cd fundus
   python3 -m venv .venv
   .venv/bin/pip install -e ".[render]"
   .venv/bin/python -m playwright install chromium-headless-shell
   ```
   Windows (PowerShell): `py -3.12 -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -e ".[render]"`.
3. **Create the archive:** `.venv/bin/fundus stats` (an empty database in `data/`).
4. **Start:** `.venv/bin/fundus serve` → portal <http://127.0.0.1:8010/portal/>, MCP <http://127.0.0.1:8010/mcp>.
   Autostart at login: `.venv/bin/fundus service install`.
5. **Connect your assistant:** `claude mcp add --transport http fundus http://127.0.0.1:8010/mcp` (other clients:
   AGENTS.md, section 5).
6. **Optional services** (nobody has to connect them; your assistant asks at installation and at every update):
   - **Your own language model** – Ollama, LM Studio, vLLM, a llama.cpp server or any OpenAI-compatible endpoint. It
     describes datasets and photos, makes search meaning-based, groups slide-search hits and answers free-text
     questions about your datasets. Set `llm_url`, `llm_api` and the models (`llm_model_text`, `llm_model_vision`,
     `llm_model_embed`) in `data/config.json`, check with `.venv/bin/fundus llm check`, then `.venv/bin/fundus embed`.
     Hardware guidance and examples: AGENTS.md, section 6.2.
   - **Gemini on Vertex AI** – AI review of your slides (numbers, sources, logic). Needs a Google Cloud project with
     billing; slides are sent to Google. Set `vertex_project` (and optionally `vertex_location`, `vertex_models`),
     sign in with `gcloud auth application-default login` or a service account, check with
     `.venv/bin/fundus vertex check`. Step by step: AGENTS.md, section 6.3.
   - **Fundus Cloud** in the portal under Verwaltung → Fundus Cloud.

## Updating

Back up (`.venv/bin/fundus snapshot`), stop the server, `git pull --ff-only`, `.venv/bin/pip install -e ".[render]"`,
run `.venv/bin/fundus stats` once (migrates the database), start the server again. What changed: `CHANGELOG.md`.
Details: AGENTS.md, section 10.

## Keep your data private

Your archive lives in `data/` (or where `FUNDUS_DB` points). It is ignored by git – never commit, upload or share it.
The server listens on 127.0.0.1 only and has no login; do not expose it to the internet.
