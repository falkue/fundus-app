# Installing Fundus

The detailed, step-by-step guide – written so that an AI coding assistant can follow it, with checks and
troubleshooting for Linux, macOS and Windows – is **[AGENTS.md](AGENTS.md)**. The easiest way: open the repository in
your assistant (Claude Code, Codex, Gemini CLI, Cursor …) and ask *"Install Fundus by following AGENTS.md."*

> Linux is tested; **macOS and Windows are untested so far**. If a step fails, please open an issue with the step and
> the error message (no archive contents, no personal data).

## In short

1. **Prerequisites:** Python 3.12 (3.11 works) and git. Optional: Ollama (local models), ffmpeg, poppler.
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
6. **Optional:** `ollama pull qwen3-embedding:8b` and `.venv/bin/fundus embed` for semantic search; Fundus Cloud in
   the portal under Verwaltung → Fundus Cloud.

## Keep your data private

Your archive lives in `data/` (or where `FUNDUS_DB` points). It is ignored by git – never commit, upload or share it.
The server listens on 127.0.0.1 only and has no login; do not expose it to the internet.
