"""Install-specific settings, the same on Linux, macOS and Windows.

Nothing user- or machine-specific is hard-coded in the code paths: every value comes from (first wins)

1. an environment variable (``FUNDUS_*``),
2. the config file – ``$FUNDUS_CONFIG``, else ``<data dir>/config.json``, else ``<user config dir>/config.json``
   (Linux ``~/.config/fundus``, macOS ``~/Library/Application Support/Fundus``, Windows ``%APPDATA%\\Fundus``),
3. what the running server announced (``<data dir>/server.json``, written at server start: bind address and public URL,
   so CLI processes and the render hook hand out the same URLs as the server),
4. a default that works on a fresh single-user install (server on 127.0.0.1:8010, projects in ``~/Projects``).

Keys of the config file: ``host``, ``port``, ``public_url``, ``allowed_hosts`` (list of ``host:port``), ``projects_root``,
``mck_skill``, ``site_label``. This module imports nothing from Fundus (store.py imports it).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010
APP = "Fundus"


# ---------------------------------------------------------------- platform folders
def platform_name(platform: str | None = None) -> str:
    """'linux', 'macos', 'windows' (or the raw sys.platform for anything else)."""
    p = platform or sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p in ("win32", "cygwin") or p.startswith("win"):
        return "windows"
    return p


def _home() -> Path:
    return Path.home()


def user_config_dir(platform: str | None = None) -> Path:
    pn = platform_name(platform)
    if pn == "windows":
        return Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming")) / APP
    if pn == "macos":
        return _home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_CONFIG_HOME") or (_home() / ".config")) / "fundus"


def user_data_dir(platform: str | None = None) -> Path:
    pn = platform_name(platform)
    if pn == "windows":
        return Path(os.environ.get("LOCALAPPDATA") or (_home() / "AppData" / "Local")) / APP
    if pn == "macos":
        return _home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_DATA_HOME") or (_home() / ".local" / "share")) / "fundus"


def cache_dir(platform: str | None = None) -> Path:
    """Where the render hook logs (Linux: ~/.cache, unchanged from render.sh)."""
    pn = platform_name(platform)
    if pn == "windows":
        return Path(os.environ.get("LOCALAPPDATA") or (_home() / "AppData" / "Local")) / APP / "Cache"
    if pn == "macos":
        return _home() / "Library" / "Caches" / APP
    return Path(os.environ.get("XDG_CACHE_HOME") or (_home() / ".cache"))


def default_db() -> Path:
    """$FUNDUS_DB, else data/fundus.sqlite in the source checkout (the usual install: a clone with `pip install -e`),
    else the user data folder (a plain `pip install` puts the package into site-packages, which is no place for data)."""
    if os.environ.get("FUNDUS_DB"):
        return Path(os.environ["FUNDUS_DB"]).expanduser()
    if (REPO_ROOT / "pyproject.toml").exists() or (REPO_ROOT / "data").is_dir():
        return REPO_ROOT / "data" / "fundus.sqlite"
    return user_data_dir() / "fundus.sqlite"


def data_dir() -> Path:
    return default_db().parent


# ---------------------------------------------------------------- config file
_cache: dict = {}


def config_file() -> Path | None:
    if os.environ.get("FUNDUS_CONFIG"):
        return Path(os.environ["FUNDUS_CONFIG"]).expanduser()
    for p in (data_dir() / "config.json", user_config_dir() / "config.json"):
        if p.is_file():
            return p
    return None


def _read_json(p: Path | None) -> dict:
    if p is None:
        return {}
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _cache.get(str(p), (None,))[0] != key:
        try:
            val = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            val = {}
        _cache[str(p)] = (key, val if isinstance(val, dict) else {})
    return _cache[str(p)][1]


def load() -> dict:
    return _read_json(config_file())


def get(key: str, env: str | None = None, default=None):
    if env and os.environ.get(env) not in (None, ""):
        return os.environ[env]
    v = load().get(key)
    return default if v in (None, "") else v


def server_info_file(data: Path | None = None) -> Path:
    return Path(data or data_dir()) / "server.json"


def server_info() -> dict:
    return _read_json(server_info_file())


# ---------------------------------------------------------------- server address
def server_host() -> str:
    return str(get("host", "FUNDUS_MCP_HOST", DEFAULT_HOST))


def server_port() -> int:
    try:
        return int(get("port", "FUNDUS_MCP_PORT", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _wildcard(host: str) -> bool:
    return host in ("0.0.0.0", "::", "", "[::]")


def url_for(host: str, port: int) -> str:
    """http://host:port for a bind address; a wildcard bind is reached by the machine's name."""
    h = socket.gethostname() if _wildcard(host) else host
    if ":" in h and not h.startswith("["):
        h = f"[{h}]"
    return f"http://{h}:{port}"


def public_url() -> str:
    """Base URL that links in answers point to (portal, slide files, photos, datasets)."""
    v = get("public_url", "FUNDUS_PUBLIC_URL") or server_info().get("public_url")
    if not v:
        v = url_for(server_host(), server_port())
    return str(v).rstrip("/")


def tailscale_names(timeout: float = 3.0) -> list[str]:
    """This machine's Tailscale DNS name, its short form and its Tailscale IPs ([] without Tailscale)."""
    exe = "tailscale.exe" if platform_name() == "windows" else "tailscale"
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=timeout)
        me = json.loads(out.stdout or "{}").get("Self") or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    names = []
    dns = (me.get("DNSName") or "").rstrip(".")
    if dns:
        names += [dns, dns.split(".", 1)[0]]
    names += [ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip]
    return names


def allowed_hosts(host: str, port: int, probe_tailscale: bool = True) -> list[str]:
    """Host headers the server accepts: its bind address, this machine's names (incl. Tailscale), localhost and the
    config's ``allowed_hosts`` / $FUNDUS_ALLOWED_HOSTS (comma-separated)."""
    extra = get("allowed_hosts", "FUNDUS_ALLOWED_HOSTS") or []
    if isinstance(extra, str):
        extra = [x.strip() for x in extra.split(",") if x.strip()]
    names = [] if _wildcard(host) else [host]
    try:
        hn = socket.gethostname()
        names += [hn, f"{hn}.local"] if hn else []
    except OSError:
        pass
    if probe_tailscale:
        names += tailscale_names()
    out = [f"{n}:{port}" for n in names if n] + list(extra)
    pu = get("public_url", "FUNDUS_PUBLIC_URL")
    if pu:
        from urllib.parse import urlsplit
        out.append(urlsplit(str(pu)).netloc)
    out += ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    return list(dict.fromkeys(out))


def announce_server(host: str, port: int, data: Path | None = None) -> str:
    """Called at server start: decide the public URL (config/env, else the bind address), make it visible to this
    process and its children (scheduler jobs) and write server.json into the data folder of the server's database
    (`data`; a server on a test or scratch database never touches the real one). Returns the URL."""
    url = (get("public_url", "FUNDUS_PUBLIC_URL") or url_for(host, port)).rstrip("/")
    os.environ["FUNDUS_PUBLIC_URL"] = url
    try:
        p = server_info_file(data)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps({"host": host, "port": port, "public_url": url, "pid": os.getpid()}), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
    return url


# ---------------------------------------------------------------- folders outside Fundus
def projects_root() -> Path:
    return Path(str(get("projects_root", "FUNDUS_PROJECTS", str(_home() / "Projects")))).expanduser()


def mck_skill_dir() -> Path:
    return Path(str(get("mck_skill", "FUNDUS_MCK_SKILL", str(_home() / ".claude" / "skills" / "mck-exhibit")))).expanduser()


def site_label() -> str:
    try:
        return str(get("site_label", "FUNDUS_SITE_LABEL", socket.gethostname()))
    except OSError:
        return "Fundus"


def display_path(p: Path) -> str:
    """Path for display, with the home folder as ~."""
    try:
        return "~/" + Path(p).resolve().relative_to(_home().resolve()).as_posix()
    except ValueError:
        return str(p)
