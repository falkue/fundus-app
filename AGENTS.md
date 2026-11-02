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
   `%USERPROFILE%\fundus` on Windows) and whether they want the optional parts (local models, autostart, Fundus Cloud).
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
| Ollama + models | optional | semantic search, descriptions of datasets and photos, name variants |
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

## 6. Optional extensions

### 6.1 Local models with Ollama (semantic search, descriptions)

Without Ollama, search is full-text and the result says `"mode": "lexical"`; everything else works. With Ollama:

| Purpose | Default model | Smaller alternative (laptops, ≤16 GB RAM) | Setting |
|---|---|---|---|
| Embeddings (semantic search) | `qwen3-embedding:8b` | `qwen3-embedding:0.6b` or `:4b` | `FUNDUS_EMBED_MODEL` (vectors are cut to 1024 dims: `FUNDUS_EMBED_DIM`) |
| Descriptions of photos and datasets, name variants, aspect groups | `gemma4:26b` (vision) | a smaller vision model, e.g. `gemma3:12b` or `gemma3:4b` | `FUNDUS_PHOTO_MODEL`, `FUNDUS_DATASET_MODEL`, `FUNDUS_ENTITY_MODEL` |
| Context length for the description model | 262144 | 32768 | `FUNDUS_LLM_NUM_CTX` |

Install Ollama from <https://ollama.com/download> (Linux: the install script asks for sudo; macOS: the app;
Windows: the installer or `winget install -e --id Ollama.Ollama`). Then:

```bash
ollama pull qwen3-embedding:8b        # or the smaller alternative
ollama pull gemma4:26b                # optional; or the smaller alternative
curl -s http://localhost:11434/api/tags | head -c 200; echo     # expect: JSON listing the models
```

When you use other models, set the environment variables for the server (in the autostart service, or in the shell
before `fundus serve`). Then embed the archive once (later runs only embed new items):

```bash
.venv/bin/fundus embed                # Windows: .venv\Scripts\fundus.exe embed
.venv/bin/fundus search "test" --mode semantic      # expect: no "fallback" field in the answer
```

Apple Silicon: Ollama uses the GPU (Metal) automatically. A 26B model needs about 20 GB of free unified memory – use
the smaller alternatives on 16 GB machines. Ollama on another machine: `FUNDUS_OLLAMA_URL=http://host:11434`.

### 6.2 Fundus Cloud (shared pool with colleagues)

Optional. In the portal: **Verwaltung → Fundus Cloud** – sign in and connect; the portal
explains what is shared. Nothing leaves the computer before the user connects and chooses what to share. Never
connect on the user's behalf.

### 6.3 More

- **Videos and PDFs:** install ffmpeg (videos in the media archive) and poppler (`pdftotext`, text of PDF sources).
  Check: `ffmpeg -version`, `pdftotext -v`.
- **Importers:** write your own for your research files – see `importers/CONTRACT.md`.
- **Configuration:** environment variables starting with `FUNDUS_` (host/port `FUNDUS_MCP_HOST`/`FUNDUS_MCP_PORT`,
  public URL `FUNDUS_PUBLIC_URL`, database `FUNDUS_DB`, project root `FUNDUS_PROJECTS`, photo credit
  `FUNDUS_PHOTO_CREDIT`, Crossref contact `FUNDUS_CONTACT_EMAIL`, models as above).

---

## 7. Verification checklist (all OS)

Run these after the installation; report each result to the user. (`<fundus>` = `.venv/bin/fundus` on Linux/macOS,
`.venv\Scripts\fundus.exe` on Windows.)

| # | Command | Expected |
|---|---|---|
| 1 | `<fundus> stats` | JSON with counts |
| 2 | `<fundus> search "test"` | JSON with `"mode": "lexical"` (or `hybrid` with Ollama) and an empty or short result list |
| 3 | portal `http://127.0.0.1:8010/portal/` in a browser | the Fundus portal opens (empty archive) |
| 4 | `http://127.0.0.1:8010/api/stats` | JSON |
| 5 | MCP client lists the server | Claude Code: `claude mcp list` shows `fundus … Connected` |
| 6 | an assistant session calls `fundus_overview` | empty lists, no error |
| 7 | `git status --short` in the clone | does not list `data/` |
| 8 | optional: `<fundus> search "test" --mode semantic` | no `fallback` (Ollama active) |
| 9 | optional: tests `.venv/bin/pip install pytest && .venv/bin/python -m pytest -q` | all pass (about 2 minutes) |

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
- **Semantic search says `fallback`:** Ollama is not running or the embedding model is not pulled (6.1).

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
