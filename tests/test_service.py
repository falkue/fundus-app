"""`fundus service` (fundus/service.py) with the platform mocked, plus the install-specific settings (fundus/config.py):
systemd units with this install's paths, the launchd agent (template = generator), the Windows task scripts, and how
host, public URL, allowed hosts and folders are derived."""
import json
import plistlib
import subprocess
from pathlib import Path

import pytest

from fundus import config
from fundus import service as sv

ROOT = Path(__file__).resolve().parent.parent


def runner_log():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    return calls, run


# ---------------------------------------------------------------- Linux
def test_linux_units_get_this_installs_paths(tmp_path):
    p = sv.plan("install", platform="linux", host="10.1.2.3", port=9000, python="/opt/fundus/.venv/bin/python",
                home=tmp_path)
    d = tmp_path / ".config" / "systemd" / "user"
    assert set(p.files) == {d / u for u in sv.LINUX_UNITS}
    mcp = p.files[d / "fundus-mcp.service"]
    assert "ExecStart=/opt/fundus/.venv/bin/python -m fundus.mcp_server --host 10.1.2.3 --port 9000" in mcp
    assert f"WorkingDirectory={ROOT}" in mcp
    for text in p.files.values():
        assert "%h/Projects" not in text and "127.0.0.1" not in text
    imp = p.files[d / "fundus-import.service"]
    assert "ExecStartPost=-/opt/fundus/.venv/bin/python -m fundus.cli exhibits sync" in imp
    assert p.files[d / "fundus-embed.timer"] == (ROOT / "deploy" / "fundus-embed.timer").read_text()
    assert p.commands[-1][:4] == ["systemctl", "--user", "enable", "--now"] and "fundus-import.timer" in p.commands[-1]
    assert sv.REVIEW_SYNC not in " ".join(p.commands[-1])
    p2 = sv.plan("install", platform="linux", home=tmp_path, with_review_sync=True)
    assert d / sv.REVIEW_SYNC in p2.files


def test_linux_units_equal_the_deploy_files_for_the_spark_layout(tmp_path):
    """With the paths of Falk's Spark the generated units say the same as deploy/ (where %h = home)."""
    home = Path("/home/user")
    t = sv.Target("linux", str(home / "Projects/Software/Fundus/.venv/bin/python"), home / "Projects/Software/Fundus",
                  home, "127.0.0.1", 8010)
    for unit in ("fundus-import.service", "fundus-cleanup.service", "fundus-snapshot.service", "fundus-embed.service"):
        t.repo = ROOT                       # read the templates from this checkout …
        text = sv.linux_unit(unit, t)
        want = (ROOT / "deploy" / unit).read_text().replace("%h", str(home))
        want = want.replace(f"WorkingDirectory={home}/Projects/Software/Fundus", f"WorkingDirectory={ROOT}")
        assert text == want, unit           # … identical apart from the checkout folder


def test_linux_install_refuses_to_overwrite_without_force(tmp_path):
    p = sv.plan("install", platform="linux", home=tmp_path, python="/py")
    calls, run = runner_log()
    res = sv.execute(p, runner=run)
    assert res["ok"] and calls[0] == ["systemctl", "--user", "daemon-reload"]
    unit = tmp_path / ".config/systemd/user/fundus-mcp.service"
    assert unit.exists()
    assert sv.execute(p, runner=run)["ok"]                     # same content: fine
    unit.write_text("[Service]\nExecStart=/somebody/else\n")
    with pytest.raises(PermissionError):
        sv.execute(p, runner=run)
    assert sv.execute(p, runner=run, force=True)["ok"]
    calls.clear()
    res = sv.execute(sv.plan("uninstall", platform="linux", home=tmp_path), runner=run)
    assert res["ok"] and not unit.exists() and calls[0][2] == "disable"


def test_dry_run_changes_nothing(tmp_path):
    p = sv.plan("install", platform="linux", home=tmp_path)
    res = sv.execute(p, dry_run=True, runner=lambda c: pytest.fail("ran a command"))
    assert res["dry_run"] and not (tmp_path / ".config").exists() and len(res["contents"]) == len(sv.LINUX_UNITS)


def test_missing_tool_is_reported(tmp_path):
    p = sv.plan("status", platform="linux", home=tmp_path)
    res = sv.execute(p, runner=lambda c: None)
    assert "systemctl not found" in res["results"][0]["error"]


# ---------------------------------------------------------------- macOS
def test_macos_launch_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "_uid", lambda: 501)
    p = sv.plan("install", platform="darwin", host="127.0.0.1", port=8010, python="/Users/x/fundus/.venv/bin/python",
                home=tmp_path)
    path = tmp_path / "Library/LaunchAgents/org.fundus.serve.plist"
    pl = plistlib.loads(p.files[path].encode())
    assert pl["ProgramArguments"] == ["/Users/x/fundus/.venv/bin/python", "-m", "fundus.cli", "serve", "--host",
                                      "127.0.0.1", "--port", "8010"]
    assert pl["RunAtLoad"] is True and pl["KeepAlive"] == {"SuccessfulExit": False}
    assert pl["StandardOutPath"] == str(tmp_path / "Library/Logs/Fundus/server.log")
    assert p.commands == [["launchctl", "bootout", "gui/501/org.fundus.serve"],
                          ["launchctl", "bootstrap", "gui/501", str(path)]]
    calls, run = runner_log()
    assert sv.execute(p, runner=run)["ok"] and path.exists()
    res = sv.execute(sv.plan("uninstall", platform="darwin", home=tmp_path), runner=run)
    assert res["ok"] and not path.exists() and calls[-1][1] == "bootout"


def test_macos_template_equals_the_generated_plist(tmp_path):
    t = sv.Target("macos", "/Users/x/py & co/python", ROOT, tmp_path, "0.0.0.0", 8123)
    assert plistlib.loads(sv.fill_mac_template(t).encode()) == sv.mac_plist(t)


# ---------------------------------------------------------------- Windows
def test_windows_task(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    py = tmp_path / "venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    p = sv.plan("install", platform="win32", python=str(py), home=tmp_path, host="127.0.0.1", port=8010)
    cmd = p.commands[0]
    assert cmd[:6] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                       str(ROOT / "deploy" / "windows" / "install-fundus-task.ps1")]
    args = dict(zip(cmd[6::2], cmd[7::2]))
    assert args["-Python"] == str(py) and args["-BindHost"] == "127.0.0.1" and args["-Port"] == "8010"
    assert args["-LogFile"] == str(tmp_path / "AppData/Local/Fundus/logs/server.log") and args["-TaskName"] == "Fundus"
    (py.parent / "pythonw.exe").write_text("")                    # hidden console when pythonw exists
    p = sv.plan("install", platform="win32", python=str(py), home=tmp_path)
    assert dict(zip(p.commands[0][6::2], p.commands[0][7::2]))["-Python"].endswith("pythonw.exe")
    un = sv.plan("uninstall", platform="win32", home=tmp_path).commands[0]
    assert un[-3].endswith("uninstall-fundus-task.ps1") and un[-1] == "Fundus"
    assert sv.plan("status", platform="win32", home=tmp_path).commands[0][:2] == ["schtasks", "/Query"]


def test_windows_scripts_take_the_parameters_the_installer_passes():
    inst = (ROOT / "deploy/windows/install-fundus-task.ps1").read_text()
    for prm in ("$Repo", "$Python", "$BindHost", "$Port", "$TaskName", "$LogFile"):
        assert prm in inst
    assert "Register-ScheduledTask" in inst and "-AtLogOn" in inst and "fundus.cli serve" in inst
    assert "$Host" not in inst.replace("$BindHost", "")            # $Host is reserved in PowerShell
    un = (ROOT / "deploy/windows/uninstall-fundus-task.ps1").read_text()
    assert "Unregister-ScheduledTask" in un and "$TaskName" in un


def test_windows_without_scripts_uses_schtasks(tmp_path, monkeypatch):
    t = sv.Target("windows", "C:/py/python.exe", tmp_path, tmp_path, "127.0.0.1", 8010)   # repo without deploy/
    p = sv._win_plan("install", t)
    assert p.commands[0][:2] == ["schtasks", "/Create"] and "fundus.cli serve" in p.commands[0][-1]


def test_unknown_platform():
    with pytest.raises(ValueError):
        sv.plan("install", platform="sunos5")
    with pytest.raises(ValueError):
        sv.plan("reboot", platform="linux")


def test_cli_service_dry_run(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "platform_name", lambda p=None: "macos")
    monkeypatch.setattr(sv.Path, "home", classmethod(lambda cls: tmp_path))
    from fundus import cli
    assert cli.main(["service", "install", "--dry-run", "--port", "8123"]) == 0
    out = capsys.readouterr().out
    assert "org.fundus.serve.plist" in out and "<string>8123</string>" in out and "$ launchctl bootstrap" in out
    assert not (tmp_path / "Library").exists()


# ---------------------------------------------------------------- config
def test_public_url_order(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_DB", str(tmp_path / "f.sqlite"))
    for k in ("FUNDUS_PUBLIC_URL", "FUNDUS_MCP_HOST", "FUNDUS_MCP_PORT", "FUNDUS_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.public_url() == "http://127.0.0.1:8010"                       # fresh install
    (tmp_path / "server.json").write_text(json.dumps({"public_url": "http://10.0.0.5:8010"}))
    assert config.public_url() == "http://10.0.0.5:8010"                        # what the running server announced
    (tmp_path / "config.json").write_text(json.dumps({"public_url": "https://fundus.example.org/", "port": 9001}))
    assert config.public_url() == "https://fundus.example.org" and config.server_port() == 9001
    monkeypatch.setenv("FUNDUS_PUBLIC_URL", "http://env:1")
    assert config.public_url() == "http://env:1"


def test_announce_server_writes_next_to_the_database(tmp_path, monkeypatch):
    monkeypatch.delenv("FUNDUS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("FUNDUS_CONFIG", str(tmp_path / "none.json"))
    url = config.announce_server("100.64.0.9", 8010, tmp_path)
    assert url == "http://100.64.0.9:8010" and json.loads((tmp_path / "server.json").read_text())["port"] == 8010
    import os
    assert os.environ["FUNDUS_PUBLIC_URL"] == url
    monkeypatch.delenv("FUNDUS_PUBLIC_URL")
    assert config.announce_server("0.0.0.0", 8010, tmp_path).startswith("http://") and "0.0.0.0" not in os.environ["FUNDUS_PUBLIC_URL"]


def test_allowed_hosts(monkeypatch):
    monkeypatch.setenv("FUNDUS_ALLOWED_HOSTS", "fundus.example.org:443, other:8010")
    monkeypatch.delenv("FUNDUS_PUBLIC_URL", raising=False)
    monkeypatch.setattr(config, "tailscale_names", lambda timeout=3.0: ["box.tail1.ts.net", "box", "100.64.0.1"])
    hosts = config.allowed_hosts("100.64.0.1", 8010)
    assert hosts[0] == "100.64.0.1:8010" and "box.tail1.ts.net:8010" in hosts and "box:8010" in hosts
    assert "fundus.example.org:443" in hosts and "localhost:*" in hosts
    assert "box:8010" not in config.allowed_hosts("127.0.0.1", 8010, probe_tailscale=False)


def test_platform_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert config.user_config_dir("win32") == tmp_path / "Roaming" / "Fundus"
    assert config.cache_dir("win32") == tmp_path / "Local" / "Fundus" / "Cache"
    assert config.user_config_dir("darwin") == tmp_path / "Library/Application Support/Fundus"
    assert config.cache_dir("darwin") == tmp_path / "Library/Caches/Fundus"
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert config.cache_dir("linux") == tmp_path / ".cache"                    # where render.sh's hook logs
    assert config.platform_name("cygwin") == "windows"


def test_transport_security_has_no_fixed_hosts(monkeypatch):
    monkeypatch.delenv("FUNDUS_ALLOWED_HOSTS", raising=False)
    from fundus import mcp_server as m
    ts = m.transport_security("127.0.0.1", 8010, probe_tailscale=False)
    assert "127.0.0.1:8010" in ts.allowed_hosts and not any("100.68" in h for h in ts.allowed_hosts)
