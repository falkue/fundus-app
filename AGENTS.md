# Installation instructions for AI coding assistants

This file is written for an AI coding assistant (Claude Code, Codex, Gemini CLI, Cursor, Copilot agent …) that installs
Fundus on a user's computer. People can follow it too. It is deliberately explicit: every step has a command and a
check with the expected output. **Do not continue while a check fails** – go to the troubleshooting section of your
operating system, fix the cause, repeat the check.

> **Tested status:** Linux (Ubuntu 24.04, x86-64 and ARM64) is tested. **macOS and Windows 10/11 are untested so far.**
> The steps for them follow the same logic and use only cross-platform tools, but expect surprises. When something
> does not work, tell the user exactly which step failed with which message, and suggest opening an issue in the
> repository (https://github.com/falkue/fundus-app/issues) with that information (no personal data, no archive contents).

## 0. Rules for the installing assistant

1. **Ask before you start** which folder the user wants Fundus in (suggestion: `~/fundus` on Linux/macOS,
   `%USERPROFILE%\fundus` on Windows) and whether they want the optional parts (autostart, Fundus Cloud). The two
   **optional services** – their own language model and Gemini on Vertex AI – are asked separately at the end
   (section 6.1), at installation **and** at every update (section 10). Nobody has to connect them; point them out.
2. **No administrator rights unless a step says so.** Everything installs into the user's home folder and a Python
   virtual environment. Use `sudo` (Linux/macOS) or an elevated PowerShell (Windows) only to install missing system
   prerequisites, and only after telling the user why.
3. **The data directory is private.** It holds the user's archive (database, slides, datasets, photos). Never commit
   it, never upload it, never send its contents to a cloud service or model, never paste database contents into chat.
   It is in `.gitignore`; keep it that way.
4. **Never create, read or print secrets** (API keys, service-account keys, passwords). If an optional extension needs
   one, the user places it; you only check that the file exists.
5. **Do not change global configuration you were not asked to change** (shell profiles, PATH, firewall, other MCP
   servers). Where a step needs such a change, show it and ask.
6. Run commands from the repository root unless a step says otherwise. Show the user each check's output.
7. Report at the end: what is installed, where the data directory is, the portal and MCP URLs, which optional parts
   are active, and anything that failed.

## 1. What gets installed

| Part | Needed | What it is |
|---|---|---|
| Python ≥ 3.12 (3.11 works) | yes | runtime |
| git | yes | to clone the repository |
| Fundus (this repo) in a virtual environment `.venv` | yes | archive, CLI `fundus`, MCP server, portal |
| Chromium headless shell (via Playwright) | recommended | renders slides and previews (`.[render]` extra) |
| Data directory | yes | `data/` inside the clone by default; another place with `FUNDUS_DB=/path/fundus.sqlite` |
| Autostart service | optional | starts the server at login (systemd user unit / launchd agent / Windows scheduled task) |
| Own language model (Ollama, LM Studio, vLLM, llama.cpp server or any OpenAI-compatible endpoint) | optional service | semantic search, descriptions of datasets and photos, free-text questions, name variants (6.2) |
| Gemini on Vertex AI (Google Cloud) | optional service | AI review of slides – numbers, sources, logic (6.3) |
| ffmpeg, poppler (`pdftotext`) | optional | videos in the media archive; text of PDF sources |
| Fundus Cloud | optional | shared pool with colleagues, connected in the portal |

The server listens on `http://127.0.0.1:8010` (only this computer). Endpoints: MCP `http://127.0.0.1:8010/mcp`,
portal `http://127.0.0.1:8010/portal/`, statistics API `http://127.0.0.1:8010/api/stats`.

---

## 2. Linux (Ubuntu/Debian; other distributions analogously)

### 2.1 Prerequisites

```bash
python3 --version          # expect: Python 3.12.x or newer (3.11 works)
git --version              # expect: git version 2.x
```

If missing (asks for the user's password – tell them first):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git sqlite3
# optional: sudo apt install -y ffmpeg poppler-utils
```

On Ubuntu older than 24.04, `python3` may be 3.10: install a newer one (`sudo apt install python3.12 python3.12-venv`,
from the deadsnakes PPA if needed) and use `python3.12` instead of `python3` below.

### 2.2 Clone and install

```bash
git clone https://github.com/falkue/fundus-app.git ~/fundus
cd ~/fundus
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[render]"
.venv/bin/python -m playwright install chromium-headless-shell
```


If Playwright complains about missing system libraries: `sudo .venv/bin/python -m playwright install-deps chromium`
(tell the user; it installs packages with apt).

Check:

```bash
.venv/bin/fundus --help | head -3        # expect: "usage: fundus ..." and a list of commands
.venv/bin/python -c "import fundus, duckdb, sqlite_vec; print('ok')"     # expect: ok
```

### 2.3 Create the archive (data directory)

```bash
.venv/bin/fundus stats
```

Check: a JSON object with counts, all `0` on a new install. The file `data/fundus.sqlite` now exists
(`ls data/` → `fundus.sqlite`).

### 2.4 Start the server

```bash
.venv/bin/fundus serve   # same as: .venv/bin/python -m fundus.mcp_server --host 127.0.0.1 --port 8010
```


It runs in the foreground. Leave it running and check from a second terminal:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/portal/       # expect: 200
curl -s http://127.0.0.1:8010/api/stats | head -c 120; echo                  # expect: JSON starting with {
```

Stop it with Ctrl+C before 2.5.

### 2.5 Autostart (optional)

```bash
.venv/bin/fundus service install          # writes and enables the systemd user units (fundus-mcp.service + timers)
loginctl enable-linger "$USER"            # optional: run without being logged in (may ask for the password)
```


Check:

```bash
systemctl --user is-active fundus-mcp     # expect: active   (verify the unit name after merge)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/portal/       # expect: 200
```

Logs: `journalctl --user -u fundus-mcp -n 50`. Remove: `.venv/bin/fundus service uninstall`.

Continue with **section 5** (MCP registration).

---

## 3. macOS (13 Ventura or newer; Apple Silicon and Intel) – untested

### 3.1 Prerequisites

Use Homebrew (<https://brew.sh>). If `brew` is missing, ask the user to install it themselves (it needs their password).

```bash
brew --version                     # expect: Homebrew 4.x
brew install python@3.12 git       # optional: brew install ffmpeg poppler
python3.12 --version               # expect: Python 3.12.x
```

Use `python3.12` explicitly: the system `python3` from the Xcode command line tools may be older. On Apple Silicon,
Homebrew lives in `/opt/homebrew`; if `python3.12` is not found, the shell is missing `eval "$(/opt/homebrew/bin/brew shellenv)"`
– show that line to the user instead of editing their profile yourself.

### 3.2 Clone and install

```bash
git clone https://github.com/falkue/fundus-app.git ~/fundus
cd ~/fundus
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[render]"                   # quotes matter in zsh: [ ] are glob characters
.venv/bin/python -m playwright install chromium-headless-shell
```


Check:

```bash
.venv/bin/fundus --help | head -3        # expect: "usage: fundus ..."
.venv/bin/python -c "import fundus, duckdb, sqlite_vec; print('ok')"     # expect: ok
```

`sqlite_vec` needs a Python whose `sqlite3` module can load extensions. Homebrew's Python can; the python.org
installer and the system Python may not – see troubleshooting 7.2.

### 3.3 Create the archive

```bash
.venv/bin/fundus stats             # expect: JSON with counts, all 0
```

### 3.4 Start the server

```bash
.venv/bin/fundus serve             # same as: .venv/bin/python -m fundus.mcp_server --host 127.0.0.1 --port 8010
```

Check from a second terminal:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/portal/       # expect: 200
```

macOS may ask once whether Python may accept incoming connections – the server only listens on 127.0.0.1, so
"Deny" is fine; nothing from outside is needed.

### 3.5 Autostart (optional)

```bash
.venv/bin/fundus service install   # writes ~/Library/LaunchAgents/org.fundus.serve.plist and loads it
```


Check: `launchctl list | grep -i fundus` shows a line with exit status `0`, and the portal check above answers `200`
after a new login. Logs: see the paths printed by `fundus service install`.

Continue with **section 5**.

---

## 4. Windows 10/11 (PowerShell) – untested

Use **PowerShell** (not cmd.exe). Paths below use `$HOME\fundus`.

### 4.1 Prerequisites

```powershell
py --version              # expect: Python 3.12.x (the py launcher); if missing try: python --version
git --version             # expect: git version 2.x
```

If missing, install with winget (asks for confirmation; tell the user first):

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
# optional: winget install -e --id Gyan.FFmpeg
```

Open a **new** PowerShell window afterwards so PATH is updated. Prefer `py -3.12` over `python`: on many machines
`python` opens the Microsoft Store stub (see 7.3).

### 4.2 Clone and install

```powershell
git clone https://github.com/falkue/fundus-app.git $HOME\fundus
cd $HOME\fundus
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[render]"
.venv\Scripts\python.exe -m playwright install chromium-headless-shell
```


Call the venv's programs by path (`.venv\Scripts\python.exe`, `.venv\Scripts\fundus.exe`); you do not need to
activate the venv. If you do activate it (`.venv\Scripts\Activate.ps1`) and PowerShell refuses with "running scripts
is disabled", see 7.3 – do not change the execution policy machine-wide.

Check:

```powershell
.venv\Scripts\fundus.exe --help | Select-Object -First 3                     # expect: "usage: fundus ..."
.venv\Scripts\python.exe -c "import fundus, duckdb, sqlite_vec; print('ok')"  # expect: ok
```

### 4.3 Create the archive

```powershell
.venv\Scripts\fundus.exe stats     # expect: JSON with counts, all 0
```

### 4.4 Start the server

```powershell
.venv\Scripts\fundus.exe serve     # same as: .venv\Scripts\python.exe -m fundus.mcp_server --host 127.0.0.1 --port 8010
```

Check from a second PowerShell window:

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8010/portal/).StatusCode    # expect: 200
```

If Windows Defender Firewall asks whether Python may communicate: "Cancel"/"Deny" is fine – the server only listens on
127.0.0.1.

### 4.5 Autostart (optional)

```powershell
.venv\Scripts\fundus.exe service install    # registers the scheduled task "Fundus" that starts at logon (deploy/windows/install-fundus-task.ps1)
```


Check: `Get-ScheduledTask | Where-Object TaskName -like '*fundus*'` lists the task with State `Ready` or `Running`; after
the next logon the portal check answers `200`.

Continue with **section 5**.

---

## 5. Register the MCP server in the assistant

The server must be running (section 2.4/3.4/4.4 or the autostart). The MCP endpoint uses **streamable HTTP** at
`http://127.0.0.1:8010/mcp`; no API key.

**Claude Code**

```bash
claude mcp add --transport http fundus http://127.0.0.1:8010/mcp            # add --scope user for all projects
claude mcp list                                                              # expect: fundus: ... ✓ Connected
```

**General shape** for clients configured with JSON (key names differ per client – check its documentation):

```json
{
  "mcpServers": {
    "fundus": { "type": "http", "url": "http://127.0.0.1:8010/mcp" }
  }
}
```

Known variants (as of late 2026; verify against the client's current docs):

| Client | Where | Entry |
|---|---|---|
| Cursor | `~/.cursor/mcp.json` or `.cursor/mcp.json` | `"fundus": {"url": "http://127.0.0.1:8010/mcp"}` under `mcpServers` |
| Gemini CLI | `~/.gemini/settings.json` | `"fundus": {"httpUrl": "http://127.0.0.1:8010/mcp"}` under `mcpServers` |
| VS Code (Copilot) | `.vscode/mcp.json` | `"fundus": {"type": "http", "url": "http://127.0.0.1:8010/mcp"}` under `servers` |
| Codex CLI | `~/.codex/config.toml` | `[mcp_servers.fundus]` with `url = "http://127.0.0.1:8010/mcp"` (or `codex mcp add`) |
| Clients with stdio only | their config | `npx -y mcp-remote http://127.0.0.1:8010/mcp` as the command |

Check: in a new session of the assistant, the tool list contains `fundus_search`, `fundus_add`, `fundus_exhibit`.
Ask it to call `fundus_overview` – on an empty archive it answers with empty lists, not an error.

**Optional: tell the assistant how to use Fundus.** The server sends short usage instructions with the MCP handshake.
Users who want every session to "look in Fundus first and write sourced findings back" can add a few lines to their
assistant's global instructions (e.g. `~/.claude/CLAUDE.md`). Show such a block to the user; edit the file only if
they ask.

---

## 6. Optional services and extensions

### 6.1 Ask about the optional services (at installation and at every update)

Fundus works completely without them. Ask the user these two questions **after** the verification checklist
(section 7) passes – and again after every update (section 10) when the service is not configured yet. Explain in
one or two sentences what each one does, say clearly that it is optional, and accept "no" or "later" without pushing.

1. **"Would you like to connect your own language model?"** It describes datasets and photos so that they can be
   found, makes search meaning-based and cross-lingual (semantic search), groups slide-search hits into aspects,
   answers free-text questions about the datasets and finds name variants of organisations. Without one, search is
   full-text and datasets/photos stay without automatic descriptions; nothing else changes. → section 6.2
2. **"Would you like AI reviews of your slides with Gemini on Vertex AI (Google Cloud)?"** Gemini checks every slide
   of a review – numbers against the sources and data points, sources, logic, language – and writes its findings into
   the feedback inbox. It needs a Google Cloud project with billing, and **slides are sent to Google**. → section 6.3

To see what is configured: `<fundus> llm check --quick` and `<fundus> vertex check --no-call` (`<fundus>` =
`.venv/bin/fundus` on Linux/macOS, `.venv\Scripts\fundus.exe` on Windows).

**Where settings go.** All settings below are keys of the config file. `<fundus> llm check` prints which file is
used; if there is none yet, create `data/config.json` in the clone (next to `fundus.sqlite`; another place:
`FUNDUS_CONFIG=/path/config.json`). It is a JSON object – **add keys, never drop keys that are already there**.
Every key can also be set as an environment variable (named in the tables); the environment wins over the file.
After changing settings, restart the server (Ctrl+C and `fundus serve` again, or restart the autostart service:
Linux `systemctl --user restart fundus-mcp`, macOS `launchctl kickstart -k gui/$(id -u)/org.fundus.serve`, Windows
`Stop-ScheduledTask -TaskName Fundus; Start-ScheduledTask -TaskName Fundus`).

### 6.2 Own language model

**Let the user propose the model.** Ask what they already run or would like to run, and on which machine. Anything
that speaks one of two APIs works:

| Server | `llm_api` | typical `llm_url` |
|---|---|---|
| Ollama (<https://ollama.com>) – simplest, serves all three roles from one place | `ollama` | `http://localhost:11434` |
| LM Studio (local server tab; load a chat/vision model **and** an embedding model) | `openai` | `http://localhost:1234/v1` |
| vLLM, llama.cpp `llama-server`, LocalAI, text-generation-webui … | `openai` | `http://localhost:8000/v1` (vLLM), `http://localhost:8080/v1` (llama.cpp) |
| A hosted OpenAI-compatible service | `openai` | its base URL (see the data-protection note below) |

Fundus uses one endpoint for three **roles**. The same model can fill text and vision:

| Role | Used for | Key (environment variable) | Default | Requirement |
|---|---|---|---|---|
| text | dataset descriptions, free-text questions, name variants, aspect groups | `llm_model_text` (`FUNDUS_LLM_MODEL_TEXT`) | `gemma4:26b` | follows instructions, writes JSON |
| vision | photo and video descriptions | `llm_model_vision` (`FUNDUS_LLM_MODEL_VISION`) | `gemma4:26b` | **must accept images** (e.g. gemma3/gemma4, qwen2.5-vl, llava, llama3.2-vision) |
| embed | semantic search | `llm_model_embed` (`FUNDUS_LLM_MODEL_EMBED`) | `qwen3-embedding:8b` | an embedding model (e.g. qwen3-embedding 0.6b/4b/8b, bge-m3, nomic-embed-text, mxbai-embed-large) |

Further keys: `llm_enabled` (`true`/`false`; `false` switches every model call off), `llm_url`, `llm_api`
(`ollama` | `openai`), `llm_embed_dim` (stored vector length, default 1024 – at most the model's own dimension, a
multiple of 8), `llm_num_ctx` (Ollama context window, default 262144 – **use 32768 or less on machines with less
than 32 GB of GPU/unified memory**), `llm_api_key` (only for endpoints that need a token: the user enters it
themselves, preferably as the environment variable `FUNDUS_LLM_API_KEY` of the server – never write, read or print it
yourself).

**Hardware guidance** (4-bit quantised models; add 1–4 GB for the context window, more with a large `llm_num_ctx`):

| Model size | GPU / unified memory | Examples | Fit |
|---|---|---|---|
| 1–4 B | 3–4 GB | `gemma3:4b`, `qwen3-embedding:0.6b` / `:4b` | laptops; descriptions are short and plainer |
| 7–14 B | 6–10 GB | `gemma3:12b`, `qwen2.5vl:7b`, `qwen3-embedding:8b` (~5 GB) | a good middle |
| 24–35 B | 18–24 GB | `gemma4:26b`, `gemma3:27b`, `qwen3:32b` | the reference setup; descriptions close to a person's |
| 70 B | 40–48 GB | `llama3.1:70b` | not needed for Fundus |

Without a GPU, models run on the CPU – usable for embeddings and small models, slow for vision (minutes per photo).
Apple Silicon uses the GPU (Metal) automatically; plan for the model plus about 30 % headroom in unified memory.
Text and vision with one model save memory (only one copy loaded).

**Data protection.** A local server keeps everything on the machine. With a hosted endpoint, dataset
descriptions (column names, sample rows), photos (which may show people) and search queries go to that provider –
tell the user before configuring it, and recommend a local server for confidential or client data.

**Steps for the assistant** (after the user has chosen):

1. Make sure the server runs and the models are there. Ollama: install from <https://ollama.com/download> (Linux:
   the install script asks for sudo; macOS: the app; Windows: `winget install -e --id Ollama.Ollama`), then
   `ollama pull <model>` for each role (e.g. `ollama pull gemma3:12b` and `ollama pull qwen3-embedding:4b`).
   Other servers: the user loads the models in their tool; ask for the exact model ids it shows.
2. Write the keys into the config file (only those that differ from the defaults), for example
   ```json
   {
     "llm_url": "http://localhost:11434",
     "llm_api": "ollama",
     "llm_model_text": "gemma3:12b",
     "llm_model_vision": "gemma3:12b",
     "llm_model_embed": "qwen3-embedding:4b",
     "llm_embed_dim": 1024,
     "llm_num_ctx": 32768
   }
   ```
   or, for LM Studio: `{"llm_api": "openai", "llm_url": "http://localhost:1234/v1", "llm_model_text": "<id>",
   "llm_model_vision": "<id>", "llm_model_embed": "<id>", "llm_embed_dim": 768}`.
3. Check:
   ```bash
   .venv/bin/fundus llm check          # Windows: .venv\Scripts\fundus.exe llm check
   ```
   Expect `Result: all good`: the endpoint answers, every model is listed, the text model returns JSON, the vision
   model sees the red test square, and the embedding dimension is at least `llm_embed_dim`. The first run loads the
   models and can take a few minutes. Fix every `WARNING` line before going on (it says what to do).
4. Restart the server (6.1), then build the semantic index once (later runs only embed new items; the autostart's
   nightly job keeps it fresh):
   ```bash
   .venv/bin/fundus embed
   .venv/bin/fundus search "test" --mode semantic      # expect: no "fallback" field in the answer
   ```
5. **Changing the embedding model later** (another model or `llm_embed_dim`) needs a re-embedding of the archive –
   vectors of different models cannot be compared. `fundus llm check` warns when the archive's active space differs
   from the configuration and prints the commands:
   ```bash
   .venv/bin/fundus embed --model <new-model> --dim <dim>              # fills the new space (resumable, can take long)
   .venv/bin/fundus embed --model <new-model> --dim <dim> --activate   # switch search over once it is complete
   .venv/bin/fundus photos embed                                       # photo descriptions in the new space
   ```
   Search keeps using the old space until `--activate`. Changing the text or vision model needs nothing else;
   descriptions made earlier stay.

Turning it off again: `"llm_enabled": false` (or `FUNDUS_LLM_ENABLED=0`) and restart.

### 6.3 Gemini on Vertex AI (AI review of slides)

Optional and paid (usage-based). **The user creates the Google Cloud project, the billing and any key themselves**;
the assistant explains each step, runs the harmless commands when asked, and checks. Slide images, slide text, the
data points a slide uses and excerpts of its sources are sent to Google for every reviewed slide – discuss this first,
especially for confidential or client slides. (Google's terms for Vertex AI say customer data is not used to train
its models; let the user check the current terms and their organisation's rules.)

1. **Google Cloud project.** In <https://console.cloud.google.com> create a project (or use an existing one) and note
   its **project id** (not the name). With the Google Cloud CLI (<https://cloud.google.com/sdk/docs/install>):
   `gcloud projects create <project-id>`.
2. **Billing and a budget alert.** Billing → link a billing account to the project. Then Billing → *Budgets & alerts*
   → a monthly budget (e.g. 20 in the account's currency) with e-mail alerts at 50 %, 90 % and 100 %. A review costs
   roughly a few cents per slide with the strongest models, less with Flash models (Google's price list is binding).
3. **Enable the API:** `gcloud services enable aiplatform.googleapis.com --project <project-id>` (or in the console:
   APIs & Services → enable "Vertex AI API").
4. **Credentials – one of two ways:**
   - *On a personal machine (simplest):* `gcloud auth application-default login` (opens the browser), then
     `gcloud auth application-default set-quota-project <project-id>`. The account needs the role
     **Vertex AI User** (`roles/aiplatform.user`) or Owner on the project.
   - *Service account (servers, unattended reviews):*
     ```bash
     gcloud iam service-accounts create fundus-review --project <project-id>
     gcloud projects add-iam-policy-binding <project-id> \
       --member "serviceAccount:fundus-review@<project-id>.iam.gserviceaccount.com" --role roles/aiplatform.user
     ```
     The **user** then creates the key and stores it in their config folder (Linux `~/.config/fundus/`, macOS
     `~/Library/Application Support/Fundus/`, Windows `%APPDATA%\Fundus\`) as `fundus-review-sa.json`, or anywhere and
     sets `vertex_key_file`: `gcloud iam service-accounts keys create <path> --iam-account
     fundus-review@<project-id>.iam.gserviceaccount.com`. Never open, print or copy the key; only check that the file
     exists. (Some organisations forbid keys – then use the first way.)
5. **Settings** in the config file:

   | Key (environment variable) | Meaning | Default |
   |---|---|---|
   | `vertex_project` (`FUNDUS_VERTEX_PROJECT`) | the project id – **always set it** | a placeholder |
   | `vertex_location` (`FUNDUS_VERTEX_LOCATION`) | `global` (the newest models are only served there) or a region such as `europe-west4` / `us-central1` if the model is offered there and data residency matters | `global` |
   | `vertex_models` (`FUNDUS_VERTEX_MODELS`) | models offered for reviews, the first is the default: `"id=Label;id=Label"` or a list of ids | a built-in list of current Gemini models |
   | `vertex_key_file` (`FUNDUS_VERTEX_KEY_FILE`) | path of a service-account key | `<config folder>/fundus-review-sa.json` |

   Example: `{"vertex_project": "my-fundus-reviews", "vertex_location": "global"}`. Model ids change over time:
   look up current ones in the Vertex AI Model Garden and put them into `vertex_models` when the built-in ones are
   refused.
6. **Check:**
   ```bash
   .venv/bin/fundus vertex check            # sends one tiny request (a fraction of a cent); --no-call skips it
   ```
   Expect `Result: ready for Gemini reviews`. A `403` means the API is not enabled, billing is missing or the account
   lacks `roles/aiplatform.user`; a `404` means the model is not served in that location. The check shows the kind of
   credentials and the key file's path, never its contents.
7. Restart the server (6.1). Reviews are started in the portal (Folien → select slides → „Mit Gemini prüfen“) or by an assistant with the MCP
   tool `fundus_review` (`action='gemini'`, or `action='loop'` for repeated rounds until the slides pass).

### 6.4 Fundus Cloud (shared pool with colleagues)

Optional. In the portal: **Verwaltung → Fundus Cloud** – sign in and connect; the portal
explains what is shared. Nothing leaves the computer before the user connects and chooses what to share. Never
connect on the user's behalf.

### 6.5 More

- **Videos and PDFs:** install ffmpeg (videos in the media archive) and poppler (`pdftotext`, text of PDF sources).
  Check: `ffmpeg -version`, `pdftotext -v`.
- **Importers:** write your own for your research files – see `importers/CONTRACT.md`.
- **Configuration:** environment variables starting with `FUNDUS_` (host/port `FUNDUS_MCP_HOST`/`FUNDUS_MCP_PORT`,
  public URL `FUNDUS_PUBLIC_URL`, database `FUNDUS_DB`, project root `FUNDUS_PROJECTS`, photo credit
  `FUNDUS_PHOTO_CREDIT`, Crossref contact `FUNDUS_CONTACT_EMAIL`) or the same keys in the config file (6.1);
  language model `llm_*` (6.2), Vertex AI `vertex_*` (6.3).

---

## 7. Verification checklist (all OS)

Run these after the installation; report each result to the user. (`<fundus>` = `.venv/bin/fundus` on Linux/macOS,
`.venv\Scripts\fundus.exe` on Windows.)

| # | Command | Expected |
|---|---|---|
| 1 | `<fundus> stats` | JSON with counts |
| 2 | `<fundus> search "test"` | JSON with `"mode": "lexical"` (or `hybrid` with a language model) and an empty or short result list |
| 3 | portal `http://127.0.0.1:8010/portal/` in a browser | the Fundus portal opens (empty archive) |
| 4 | `http://127.0.0.1:8010/api/stats` | JSON |
| 5 | MCP client lists the server | Claude Code: `claude mcp list` shows `fundus … Connected` |
| 6 | an assistant session calls `fundus_overview` | empty lists, no error |
| 7 | `git status --short` in the clone | does not list `data/` |
| 8 | optional service: `<fundus> llm check`, then `<fundus> search "test" --mode semantic` | `Result: all good`; no `fallback` |
| 8b | optional service: `<fundus> vertex check` | `Result: ready for Gemini reviews` |
| 9 | optional: tests `.venv/bin/pip install pytest && .venv/bin/python -m pytest -q` | all pass (about 2 minutes) |

When rows 1–7 pass, ask the two optional-services questions (section 6.1) – unless the user has already answered them.

## 8. Troubleshooting

### 8.1 All systems

- **Port 8010 in use** (`address already in use`, `WinError 10048`): another Fundus is running, or another program uses
  the port. Find it (Linux/macOS `lsof -i :8010`; Windows `Get-NetTCPConnection -LocalPort 8010`). Either stop it or run
  on another port (`--port 8011`, or `FUNDUS_MCP_PORT=8011`) and set `FUNDUS_PUBLIC_URL=http://127.0.0.1:8011`; use the
  same port in the MCP registration.
- **MCP client cannot connect:** is the server running (portal check)? Is the URL exactly `http://127.0.0.1:8010/mcp`
  (with `/mcp`)? The server accepts the host names `127.0.0.1` and `localhost` only; another name (the computer's name,
  an IP address) is refused with "Invalid Host header" (DNS-rebinding protection) – use `127.0.0.1`. Access from another computer is off by design; enabling it (`--host` on a private network
  address, e.g. a VPN) is the user's decision.
- **`pip install` fails building a package** (`pillow-heif`, `rawpy`, `sqlite-vec`, `duckdb`): use Python 3.12 (wheels
  exist for it on all three systems); upgrade pip first; on ARM Linux make sure it is a 64-bit Python.
- **`database is locked`:** two processes write at once (e.g. a long import while the server writes). Wait and retry;
  do not delete `-wal`/`-shm` files while the server runs.
- **Semantic search says `fallback`:** the language-model server is not running, the embedding model is not pulled
  or the archive was never embedded – `<fundus> llm check` says which (6.2).
- **`fundus llm check`: vision FAILED or "did not see the red square"** – the vision model has no image input; pick a
  vision model for `llm_model_vision`. **Out of memory / very slow** – smaller models or a smaller `llm_num_ctx`.
- **`fundus vertex check`: 403 / 404** – see 6.3, step 6.

### 8.2 macOS

- **`zsh: no matches found: .[render]`** – quote it: `".[render]"`.
- **`python3.12: command not found`** on Apple Silicon – Homebrew is not on PATH (3.1).
- **`AttributeError: 'sqlite3.Connection' object has no attribute 'enable_load_extension'`** – this Python was built
  without loadable SQLite extensions (python.org installer, some pyenv builds). Recreate the venv with Homebrew's
  `python3.12`. Fundus still works without `sqlite_vec`, but semantic search does not.
- **Intel vs Apple Silicon mix:** a Python running under Rosetta (x86-64) on an M-series Mac installs x86 wheels and
  is slow. Check `python3.12 -c "import platform; print(platform.machine())"` → `arm64` on Apple Silicon.
- **Gatekeeper blocks the Chromium download** – rerun `playwright install chromium-headless-shell`; if blocked, allow it
  in System Settings → Privacy & Security (user's decision).

### 8.3 Windows

- **`python` opens the Microsoft Store or prints nothing** – the Store alias. Use `py -3.12`, or turn off the aliases
  in Settings → Apps → Advanced app settings → App execution aliases (user's decision).
- **`running scripts is disabled on this system`** when activating the venv – not needed: call
  `.venv\Scripts\python.exe` directly. If the user wants activation: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
  (ask first; never `-Scope LocalMachine`).
- **Long paths** (`FileNotFoundError` / `[Errno 2]` deep inside `site-packages` or `node_modules`): clone into a short
  folder (`C:\fundus` or `$HOME\fundus`), or let the user enable long paths (admin PowerShell:
  `New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force`,
  then reboot) – ask first. Also `git config --global core.longpaths true` (ask first).
- **Line endings:** if git converts files to CRLF and a shell script fails, clone with
  `git clone -c core.autocrlf=input …`.
- **`curl` is an alias** for `Invoke-WebRequest` in Windows PowerShell 5 – use `curl.exe` or the
  `Invoke-WebRequest` checks given above.
- **Antivirus slows down or blocks** the venv or Chromium: exclude the Fundus folder only if the user agrees.
- **Firewall prompts** – see 4.4; the server does not need incoming connections.

### 8.4 Linux

- **`ensurepip is not available`** – `sudo apt install python3-venv` (or `python3.12-venv`).
- **Playwright: `Host system is missing dependencies`** – `sudo .venv/bin/python -m playwright install-deps chromium`.
- **Service stops when logging out** – `loginctl enable-linger "$USER"`.
- **`systemctl --user` fails over SSH** (`Failed to connect to bus`) – log in once locally, or
  `export XDG_RUNTIME_DIR=/run/user/$(id -u)`.

## 9. What the assistant must NOT do

- Run `sudo` / an elevated shell without telling the user why, or for anything inside the Fundus folder.
- Commit, push, upload, sync or share the data directory (`data/`, or wherever `FUNDUS_DB` points), its backups, or
  exports of it; or read its contents into a chat or a cloud model.
- Remove `data/` from `.gitignore`, or `git add -f` anything inside it.
- Expose the server to the internet (`--host 0.0.0.0` on a public interface, port forwarding, tunnels) – the server has
  no login.
- Create, read, print or commit keys or tokens; set up cloud accounts or billing for the user.
- Connect Fundus Cloud or change sharing settings on the user's behalf.
- Edit the user's global assistant instructions, shell profile or system settings without asking.
- Delete an existing data directory or database – also not "to start fresh". Ask; suggest a backup first
  (`<fundus> snapshot`).
- Send archive contents to a hosted language model or to Vertex AI without the user having chosen that service (6.2, 6.3).

## 10. Updating an existing installation

When the user asks to update Fundus (or you find an existing clone with a `data/` directory), do **not** reinstall –
update in place. The archive stays where it is; schema migrations are additive and run automatically.

1. **Back up first:** `<fundus> snapshot` (a consistent copy of the database; it prints the path).
2. **Stop the server** (Ctrl+C, or the autostart: Linux `systemctl --user stop fundus-mcp`, macOS
   `launchctl bootout gui/$(id -u)/org.fundus.serve`, Windows `Stop-ScheduledTask -TaskName Fundus`).
3. **Pull and reinstall the package** (dependencies may have changed):
   ```bash
   git pull --ff-only
   .venv/bin/pip install -e ".[render]"        # Windows: .venv\Scripts\python.exe -m pip install -e ".[render]"
   ```
   If `git pull` reports local changes, show them to the user and ask – never discard them.
4. **Run the migrations by starting once:** `<fundus> stats` (opens the database and migrates it; expect JSON with
   the user's counts – not zeros), then start the server again (`fundus serve`, or Linux `systemctl --user start
   fundus-mcp`, macOS `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.fundus.serve.plist`, Windows
   `Start-ScheduledTask -TaskName Fundus`) and repeat rows 3–6 of the checklist (section 7).
5. **Tell the user what is new:** the top section of `CHANGELOG.md`.
6. **Optional services – ask again only if not configured:**
   - run `<fundus> llm check --quick` and `<fundus> vertex check --no-call`;
   - a service is **configured** when its check passes (language model: endpoint and models answer, with keys in the
     config file or the defaults; Vertex: `vertex_project` is set and the credentials check is ok) – then only report
     its state, do not ask;
   - a service the user **declined** earlier is recorded in the config file as `"optional_services": {"llm":
     "declined", "vertex": "declined"}` (Fundus ignores this key; it is a note for assistants) – do not ask again
     unless the user brings it up;
   - otherwise ask the question from section 6.1. When the user says "no", record it as above; "later" records
     nothing, so the next update asks again.
   - A configured language model whose check now fails (model removed, server not running): report it and offer to
     fix it; never switch it off on your own.
