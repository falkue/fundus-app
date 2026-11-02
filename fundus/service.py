"""`fundus service install|uninstall|status`: start Fundus with the machine, per platform.

- Linux: the systemd user units of deploy/ (fundus-mcp.service plus the timers for import, embed, cleanup and
  snapshot), with the paths of THIS install (repository folder, interpreter) instead of the ones in the files; written
  to ~/.config/systemd/user and enabled. The timers do the jobs, so the in-process scheduler stays off.
- macOS: a launchd agent ~/Library/LaunchAgents/org.fundus.serve.plist (template deploy/macos/) running `fundus serve`
  at login; the scheduler inside the server does the jobs.
- Windows: a Task Scheduler task "Fundus" at logon (deploy/windows/install-fundus-task.ps1), running `fundus serve`
  hidden with a log file; the scheduler inside the server does the jobs.

Every action can be shown first with --dry-run (prints files and commands, changes nothing). An existing, different
installation is only replaced with --force.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fundus import config

LABEL = "org.fundus.serve"
TASK = "Fundus"
LINUX_UNITS = ("fundus-mcp.service", "fundus-import.service", "fundus-import.timer", "fundus-embed.service",
               "fundus-embed.timer", "fundus-cleanup.service", "fundus-cleanup.timer", "fundus-snapshot.service",
               "fundus-snapshot.timer")
ENABLE = ("fundus-mcp.service", "fundus-import.timer", "fundus-embed.timer", "fundus-cleanup.timer", "fundus-snapshot.timer")
REVIEW_SYNC = "fundus-review-sync.service"      # Fundus Review for people (E4, Firebase) – only with --with-review-sync


@dataclass
class Plan:
    platform: str
    files: dict = field(default_factory=dict)       # path -> content to write
    commands: list = field(default_factory=list)    # argv lists, run in order
    remove: list = field(default_factory=list)      # paths to delete (uninstall)
    ignore_errors: set = field(default_factory=set)  # indices of commands whose failure is fine
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"platform": self.platform, "files": sorted(str(p) for p in self.files), "remove": [str(p) for p in self.remove],
                "commands": [" ".join(c) for c in self.commands], "notes": self.notes}


@dataclass
class Target:
    """Where and how this install runs (all derived; tests pass their own)."""
    platform: str
    python: str
    repo: Path
    home: Path
    host: str
    port: int

    @classmethod
    def here(cls, platform: str | None = None, host: str | None = None, port: int | None = None,
             python: str | None = None, home: Path | None = None) -> "Target":
        return cls(platform=config.platform_name(platform), python=python or sys.executable, repo=config.REPO_ROOT,
                   home=Path(home) if home else Path.home(), host=host or config.server_host(),
                   port=int(port or config.server_port()))


# ---------------------------------------------------------------- Linux (systemd)
def systemd_dir(t: Target) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") if t.home == Path.home() else None
    return Path(xdg or (t.home / ".config")) / "systemd" / "user"


def linux_unit(name: str, t: Target) -> str:
    """deploy/<name> with this install's paths: the interpreter and the repository folder replace the %h/Projects/…
    paths of Falk's Spark, and the server listens on t.host:t.port."""
    text = (t.repo / "deploy" / name).read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        if line.startswith(("ExecStart", "ExecStartPost", "ExecStartPre")):
            key, _, cmd = line.partition("=")
            dash = "-" if cmd.startswith("-") else ""
            cmd = cmd[len(dash):]
            parts = cmd.split(" ", 1)
            if parts[0].endswith("/python") or parts[0].endswith("/python3"):
                parts[0] = t.python
            cmd = " ".join(parts)
            if name == "fundus-mcp.service":
                cmd = re.sub(r"--host \S+", f"--host {t.host}", cmd)
                cmd = re.sub(r"--port \d+", f"--port {t.port}", cmd)
            line = f"{key}={dash}{cmd}"
        elif line.startswith("WorkingDirectory="):
            line = f"WorkingDirectory={t.repo}"
        elif line.startswith("Description=") and name == "fundus-mcp.service":
            line = f"Description=Fundus MCP server (evidence archive, streamable-http, {t.host}:{t.port})"
        out.append(line)
    return "\n".join(out) + "\n"


def _linux_plan(action: str, t: Target, with_review_sync: bool) -> Plan:
    units = LINUX_UNITS + ((REVIEW_SYNC,) if with_review_sync else ())
    enable = ENABLE + ((REVIEW_SYNC,) if with_review_sync else ())
    d = systemd_dir(t)
    p = Plan("linux")
    sc = ["systemctl", "--user"]
    if action == "install":
        p.files = {d / u: linux_unit(u, t) for u in units}
        p.commands = [sc + ["daemon-reload"], sc + ["enable", "--now", *enable]]
        p.notes.append("the systemd timers run import, embed, tidy and snapshot – the in-process scheduler stays off")
    elif action == "uninstall":
        p.commands = [sc + ["disable", "--now", *enable]]
        p.ignore_errors = {0}
        p.remove = [d / u for u in units]
        p.commands.append(sc + ["daemon-reload"])
    else:
        p.commands = [sc + ["is-active", *enable], sc + ["is-enabled", *enable]]
        p.ignore_errors = {0, 1}
    return p


# ---------------------------------------------------------------- macOS (launchd)
def mac_plist_path(t: Target) -> Path:
    return t.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def mac_log(t: Target) -> Path:
    return t.home / "Library" / "Logs" / "Fundus" / "server.log"


def mac_plist(t: Target) -> dict:
    """The launchd job (same keys and values as deploy/macos/org.fundus.serve.plist with its placeholders filled)."""
    log = str(mac_log(t))
    return {"Label": LABEL,
            "ProgramArguments": [t.python, "-m", "fundus.cli", "serve", "--host", t.host, "--port", str(t.port)],
            "WorkingDirectory": str(t.repo), "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
            "StandardOutPath": log, "StandardErrorPath": log}


def fill_mac_template(t: Target) -> str:
    """deploy/macos/org.fundus.serve.plist with the placeholders filled (for installing by hand)."""
    from xml.sax.saxutils import escape
    text = (t.repo / "deploy" / "macos" / f"{LABEL}.plist").read_text(encoding="utf-8")
    for k, v in (("__PYTHON__", t.python), ("__HOST__", t.host), ("__PORT__", str(t.port)), ("__REPO__", str(t.repo)),
                 ("__LOG__", str(mac_log(t)))):
        text = text.replace(k, escape(v))
    return text


def _uid() -> int:
    return getattr(os, "getuid", lambda: 501)()


def _mac_plan(action: str, t: Target) -> Plan:
    p = Plan("macos")
    path, dom = mac_plist_path(t), f"gui/{_uid()}"
    if action == "install":
        p.files = {path: plistlib.dumps(mac_plist(t)).decode("utf-8")}
        p.commands = [["launchctl", "bootout", f"{dom}/{LABEL}"], ["launchctl", "bootstrap", dom, str(path)]]
        p.ignore_errors = {0}                      # not loaded yet
        p.notes += [f"log: {mac_log(t)}", "the scheduler inside the server runs import, embed, tidy and snapshot"]
    elif action == "uninstall":
        p.commands = [["launchctl", "bootout", f"{dom}/{LABEL}"]]
        p.ignore_errors = {0}
        p.remove = [path]
    else:
        p.commands = [["launchctl", "print", f"{dom}/{LABEL}"]]
        p.ignore_errors = {0}
    return p


# ---------------------------------------------------------------- Windows (Task Scheduler)
def win_log(t: Target) -> Path:
    base = os.environ.get("LOCALAPPDATA") if t.home == Path.home() else None
    return Path(base or (t.home / "AppData" / "Local")) / "Fundus" / "logs" / "server.log"


def win_python(t: Target) -> str:
    """pythonw.exe next to python.exe when it exists (no console window), else the interpreter itself."""
    py = Path(t.python)
    w = py.with_name("pythonw.exe")
    return str(w) if py.name.lower() == "python.exe" and w.exists() else t.python


def _ps(script: Path, *args: str) -> list[str]:
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]


def _win_plan(action: str, t: Target) -> Plan:
    p = Plan("windows")
    d = t.repo / "deploy" / "windows"
    if action == "install":
        script = d / "install-fundus-task.ps1"
        if script.is_file():
            p.commands = [_ps(script, "-Repo", str(t.repo), "-Python", win_python(t), "-BindHost", t.host, "-Port",
                              str(t.port), "-TaskName", TASK, "-LogFile", str(win_log(t)))]
        else:                                       # package without deploy/: the same task with schtasks
            tr = f'"{win_python(t)}" -m fundus.cli serve --host {t.host} --port {t.port} --log-file "{win_log(t)}"'
            p.commands = [["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", TASK, "/TR", tr],
                          ["schtasks", "/Run", "/TN", TASK]]
        p.notes += [f"log: {win_log(t)}", "the scheduler inside the server runs import, embed, tidy and snapshot"]
    elif action == "uninstall":
        script = d / "uninstall-fundus-task.ps1"
        p.commands = ([_ps(script, "-TaskName", TASK)] if script.is_file()
                      else [["schtasks", "/End", "/TN", TASK], ["schtasks", "/Delete", "/F", "/TN", TASK]])
        if not script.is_file():
            p.ignore_errors = {0}
    else:
        p.commands = [["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"]]
        p.ignore_errors = {0}
    return p


# ---------------------------------------------------------------- entry points
def plan(action: str, platform: str | None = None, host: str | None = None, port: int | None = None,
         python: str | None = None, home: Path | None = None, with_review_sync: bool = False) -> Plan:
    if action not in ("install", "uninstall", "status"):
        raise ValueError("action: install | uninstall | status")
    t = Target.here(platform, host, port, python, home)
    if t.platform == "linux":
        return _linux_plan(action, t, with_review_sync)
    if t.platform == "macos":
        return _mac_plan(action, t)
    if t.platform == "windows":
        return _win_plan(action, t)
    raise ValueError(f"no service installer for platform {t.platform!r} – start `fundus serve` yourself")


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    if not shutil.which(cmd[0]):
        return None
    return subprocess.run(cmd, capture_output=True, text=True)


def execute(p: Plan, dry_run: bool = False, force: bool = False, runner=None) -> dict:
    """Write/remove the files and run the commands of a plan. Refuses to overwrite a different existing file without
    force. runner(cmd) -> CompletedProcess|None (tests)."""
    runner = runner or _run
    out = p.as_dict()
    if dry_run:
        out["dry_run"] = True
        out["contents"] = {str(k): v for k, v in p.files.items()}
        return out
    changed = [str(k) for k, v in p.files.items() if k.exists() and k.read_text(encoding="utf-8") != v]
    if changed and not force:
        raise PermissionError("already installed with other content: " + ", ".join(changed) +
                              " – check with --dry-run, replace with --force")
    for path, content in p.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for path in p.remove:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
    results, ok = [], True
    for i, cmd in enumerate(p.commands):
        r = runner(cmd)
        if r is None:
            results.append({"command": " ".join(cmd), "error": f"{cmd[0]} not found"})
            ok = ok and i in p.ignore_errors
            continue
        results.append({"command": " ".join(cmd), "exit": r.returncode,
                        "output": ((r.stdout or "") + (r.stderr or "")).strip()[-2000:] or None})
        if r.returncode != 0 and i not in p.ignore_errors:
            ok = False
            break
    out.update({"ok": ok, "results": results})
    return out


def main(action: str, dry_run: bool = False, force: bool = False, host: str | None = None, port: int | None = None,
         with_review_sync: bool = False) -> dict:
    p = plan(action, host=host, port=port, with_review_sync=with_review_sync)
    return execute(p, dry_run=dry_run or False, force=force)
