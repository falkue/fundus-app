"""fundus.render (render.sh in Python) with Playwright mocked: sizes, modes, the fit line, browser flags, the
registration hook (dry run, foreground into a temp database, background), caller detection and the CLI."""
import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

from fundus import cli
from fundus import exhibits as ex
from fundus import render as rd
from test_exhibits import page, projects  # noqa: F401  (fixture)


class FakePage:
    def __init__(self, browser):
        self.b = browser
        self.context = self

    # context API used by _cdp
    def new_cdp_session(self, page):
        return FakeCDP(self.b)

    def goto(self, url, wait_until=None, timeout=None):
        self.b.urls.append(url)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *a, **k):
        pass

    def evaluate(self, js):
        return True

    def screenshot(self, path, clip):
        self.b.clips.append(clip)
        w, h = int(clip["width"] * self.b.scale), int(clip["height"] * self.b.scale)
        img = Image.new("RGB", (w, h), (255, 255, 255))
        if self.b.ink:                       # something drawn at the top left (posters get cropped to it)
            for x in range(50):
                for y in range(20):
                    img.putpixel((x, y), (0, 0, 0))
        img.save(path)

    def content(self):
        return f'<html><body data-fit="{self.b.fit}"></body></html>'

    def set_viewport_size(self, vp):
        pass


class FakeCDP:
    def __init__(self, b):
        self.b, self.handlers = b, {}

    def on(self, ev, fn):
        self.handlers[ev] = fn

    def send(self, method, params=None):
        self.b.cdp.append((method, params))
        if method == "Emulation.setVirtualTimePolicy" and params.get("budget"):
            self.handlers["Emulation.virtualTimeBudgetExpired"]({})


class FakeContext:
    def __init__(self, b):
        self.b = b

    def new_page(self):
        return FakePage(self.b)

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, args, fit, ink):
        self.args, self.fit, self.ink = args, fit, ink
        self.scale = float(next(a.split("=")[1] for a in args if a.startswith("--force-device-scale-factor=")))
        self.urls, self.clips, self.cdp, self.contexts = [], [], [], []

    def new_context(self, **kw):
        self.contexts.append(kw)
        return FakeContext(self)

    def close(self):
        pass


def fake_playwright(monkeypatch, fit=0.8, ink=True, fail_bundled=False):
    launches = []

    class Chromium:
        executable_path = "/nonexistent/headless_shell"

        def launch(self, headless=True, args=(), **kw):
            launches.append({"args": list(args), **kw})
            if fail_bundled and not kw:
                raise RuntimeError("Executable doesn't exist\nmore")
            b = FakeBrowser(list(args), fit, ink)
            launches[-1]["browser"] = b
            return b

    class PW:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rd, "_playwright", lambda: (lambda: PW()))
    return launches


@pytest.fixture
def no_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(rd, "register", lambda html, png, dom, **kw: calls.append((html, png, dom, kw)) or {"registered": True})
    monkeypatch.setattr(rd, "density_line", lambda html, dom: None)
    return calls


def test_slide_mode_matches_render_sh(tmp_path, monkeypatch, no_hook, capsys):
    launches = fake_playwright(monkeypatch, fit=0.8)
    html = tmp_path / "s1.html"
    html.write_text("<html></html>")
    r = rd.render(html)
    assert r.png == str(tmp_path / "s1.png") and r.size == (3840, 2160) and r.fit == 0.8
    assert "--force-device-scale-factor=2" in launches[0]["args"] and "--window-size=1920,1080" in launches[0]["args"]
    assert "--hide-scrollbars" in launches[0]["args"]
    out = capsys.readouterr().out
    assert out.startswith(f"{tmp_path / 's1.png'} (3840, 2160) fit=0.80 WARN: content shrunk below 85%, condense the slide")
    assert no_hook and no_hook[0][2].startswith("<html><body data-fit")          # DOM handed to the hook
    b = launches[0]["browser"]
    assert ("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 0,
                                                    "mobile": False}) in b.cdp     # size pinned, scale from the flag
    assert any(m == "Emulation.setVirtualTimePolicy" and p.get("budget") == 3000 for m, p in b.cdp)
    assert b.contexts == [{"no_viewport": True}] and b.clips == [{"x": 0, "y": 0, "width": 1920, "height": 1080}]


def test_modes_sizes_and_registration_only_for_slides(tmp_path, monkeypatch, no_hook, capsys):
    fake_playwright(monkeypatch, fit=1.0)
    html = tmp_path / "p.html"
    html.write_text("<html></html>")
    r = rd.render(html, tmp_path / "li.png", mode="linkedin")
    assert r.size == (2160, 2700) and not no_hook
    assert rd.render(html, tmp_path / "sq.png", mode="linkedin-square").size == (2160, 2160)
    assert rd.render(html, tmp_path / "ls.png", mode="linkedin-landscape").size == (2400, 1254)
    poster = rd.render(html, tmp_path / "poster.png", mode="1800")
    assert poster.size == (50 + 40, 20 + 30)                                     # white space cropped (+40/+30)
    assert not no_hook
    rd.render(html, tmp_path / "s.png", scale=1)
    assert Image.open(tmp_path / "s.png").size == (1920, 1080) and len(no_hook) == 1
    with pytest.raises(ValueError):
        rd.render(html, mode="tiktok")
    out = capsys.readouterr().out
    assert "fit=1.00 \n" in out or "fit=1.00 " in out


def test_one_browser_for_many_pages_and_fallback_channels(tmp_path, monkeypatch, no_hook):
    launches = fake_playwright(monkeypatch, fail_bundled=True)
    pages = []
    for n in ("a", "b", "c"):
        (tmp_path / f"{n}.html").write_text("<html></html>")
        pages.append((tmp_path / f"{n}.html", None))
    res = rd.render_many(pages, quiet=True)
    assert [Path(r.png).name for r in res] == ["a.png", "b.png", "c.png"]
    assert len(launches) == 2 and launches[1].get("channel") == "chrome"        # bundled missing → system Chrome
    monkeypatch.setenv("FUNDUS_CHROMIUM", "/opt/chrome")
    launches.clear()
    rd.render_many(pages[:1], quiet=True)
    assert launches[0]["executable_path"] == "/opt/chrome"


def test_missing_playwright_gives_install_hint(tmp_path, monkeypatch):
    import builtins
    real = builtins.__import__

    def imp(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", imp)
    with pytest.raises(RuntimeError, match="fundus\\[render\\]|\\[render\\]"):
        rd._playwright()


def test_pairs_from_args():
    assert rd.pairs_from_args(["a.html", "a.png", "b.html"]) == [("a.html", "a.png"), ("b.html", None)]
    with pytest.raises(ValueError):
        rd.pairs_from_args(["x.png"])
    with pytest.raises(ValueError):
        rd.pairs_from_args([])


# ---------------------------------------------------------------- registration (render.sh hook)
def _slide(root, rel="Clients/26_Northwind/exhibits/g1"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.with_suffix(".html").write_text(page("Hospitals govern AI through IT"))
    Image.new("RGB", (3840, 2160), (255, 255, 255)).save(p.with_suffix(".png"))
    return p.with_suffix(".html"), p.with_suffix(".png")


def test_register_dry_run_is_the_hook_command(projects, tmp_path, monkeypatch):
    html, png = _slide(projects)
    script = tmp_path / "build.py"
    script.write_text("print(1)")
    res = rd.register(html, png, "<body data-fit='1'>", caller=str(script), db="/x/f.sqlite", dry_run=True)
    cmd = res["command"]
    assert cmd[1:] == ["-m", "fundus.cli", "--db", "/x/f.sqlite", "exhibits", "add", str(html), str(png), "--hook",
                       "--dom", "<dom file>", "--caller", str(script)]
    monkeypatch.setenv("FUNDUS_EXHIBIT_PY", "/venv/bin/python")
    assert rd.register(html, png, None, dry_run=True)["command"][0] == "/venv/bin/python"


def test_register_skips_outside_projects(projects, tmp_path, monkeypatch):
    html, png = _slide(tmp_path / "elsewhere")
    assert rd.register(html, png, None, dry_run=True)["registered"] is False
    monkeypatch.setenv("FUNDUS_EXHIBIT_FORCE", "1")
    assert rd.register(html, png, None, dry_run=True).get("dry_run") is True


def test_register_in_foreground_into_a_temp_db(projects, tmp_path, monkeypatch):
    """The real hook (fundus.cli exhibits add --hook) in a subprocess against a temporary database."""
    monkeypatch.setenv("FUNDUS_PROJECTS", str(projects))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    html, png = _slide(projects)
    db = tmp_path / "f.sqlite"
    res = rd.register(html, png, '<html><body data-fit="0.912"></body></html>', db=str(db), wait=True)
    assert res["registered"] is True and res["result"]["status"] == "new_exhibit", res
    assert res["result"]["project"] == "northwind"
    import sqlite3
    con = sqlite3.connect(db)
    assert con.execute("SELECT fit FROM exhibit_version").fetchone()[0] == pytest.approx(0.912)
    con.close()
    assert not list((tmp_path / "cache").glob("fundus-exhibit-dom.*"))          # DOM file removed


def test_register_in_background_detaches_and_logs(projects, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    html, png = _slide(projects)
    started = []

    class P:
        def __init__(self, cmd, **kw):
            started.append((cmd, kw))
    monkeypatch.setattr(rd.subprocess, "Popen", P)
    res = rd.register(html, png, "<body>", db=str(tmp_path / "f.sqlite"))
    assert res["background"] is True and res["log"].endswith("fundus-exhibit-hook.log")
    cmd, kw = started[0]
    assert "--hook" in cmd and kw.get("start_new_session") is True and kw["stdin"] is rd.subprocess.DEVNULL
    dom_file = cmd[cmd.index("--dom") + 1]
    assert Path(dom_file).read_text() == "<body>" and Path(dom_file).parent == tmp_path / "cache"


def test_detach_flags_on_windows(monkeypatch):
    monkeypatch.setattr(rd.os, "name", "nt")
    kw = rd._detach_kwargs()
    assert "creationflags" in kw and kw["creationflags"] & 0x8 and "start_new_session" not in kw


def test_find_caller(tmp_path, monkeypatch):
    s = tmp_path / "make_slides.py"
    s.write_text("")
    assert rd.find_caller(str(s)) == str(s.resolve())
    monkeypatch.setenv("FUNDUS_EXHIBIT_SCRIPT", str(s))
    assert rd.find_caller() == str(s.resolve())
    monkeypatch.delenv("FUNDUS_EXHIBIT_SCRIPT")
    monkeypatch.setattr(rd, "_parent_script", lambda: None)
    monkeypatch.setattr(sys, "argv", [str(s)])
    assert rd.find_caller(in_process=True) == str(s.resolve())
    assert rd.find_caller(in_process=False) is None


def test_render_sh_or_python(monkeypatch, tmp_path):
    monkeypatch.setenv("FUNDUS_MCK_SKILL", str(tmp_path))
    assert rd.render_sh() is None and "fundus.cli render" in rd.render_command()   # no skill: Python renderer
    (tmp_path / "render.sh").write_text("#!/bin/bash\n")
    if rd.shutil.which("bash"):
        assert rd.render_sh() == tmp_path / "render.sh"
    monkeypatch.setattr(rd.os, "name", "nt")
    assert rd.render_sh() is None                                                  # Windows: never bash


def test_cli_render(tmp_path, monkeypatch, no_hook, capsys):
    fake_playwright(monkeypatch, fit=0.95)
    html = tmp_path / "s.html"
    html.write_text("<html></html>")
    assert cli.main(["render", str(html), str(tmp_path / "out" / "s.png"), "--no-register"]) == 0
    assert (tmp_path / "out" / "s.png").exists() and not no_hook
    assert "fit=0.95" in capsys.readouterr().out
    assert cli.main(["render", str(tmp_path / "missing.html")]) == 1
    assert cli.main(["--db", str(tmp_path / "f.sqlite"), "render", str(html)]) == 0
    assert no_hook[-1][3]["db"] == str(tmp_path / "f.sqlite")


def test_measure_many_falls_back_to_playwright_chromium(monkeypatch):
    class Mod:
        CANDIDATES = []

        @staticmethod
        def headless():
            return None

        @staticmethod
        def measure(p, h):
            return {"h": h}
    monkeypatch.setattr(ex, "_density_mod", lambda: Mod)
    monkeypatch.setattr(rd, "chromium_executable", lambda: "/pw/headless_shell")
    assert ex.measure_many(["a.html"]) == {"a.html": {"h": "/pw/headless_shell"}}
