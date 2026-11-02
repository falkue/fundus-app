"""Render slides HTML → PNG with Playwright – the mck-exhibit skill's render.sh in Python, for every platform.

    fundus render <html> [<png>] [<html> [<png>] …] [--mode slide|linkedin|linkedin-square|linkedin-landscape|<width>]
                  [--scale N] [--no-register] [--dry-run] [--wait] [--script PY] [--db PATH]
    fundus render --check <html|dir> …          deck audit without PNGs (the skill's density check, like render.sh --check)

Same behaviour as render.sh (~/.claude/skills/mck-exhibit/render.sh, not changed by Fundus):
- slide (default): viewport 1920×1080, device scale 2 → a 3840×2160 PNG of the viewport, no crop; the page's JavaScript
  gets a virtual-time budget of 3 s (Chromium's --virtual-time-budget=3000); prints `<png> (w, h) fit=0.97 [WARN …]`
  and the skill's density verdict; then registers the render in Fundus in the background exactly like the bash hook:
  `<python> -m fundus.cli exhibits add <html> <png> --hook [--dom FILE] [--caller SCRIPT]`, output appended to
  <cache>/fundus-exhibit-hook.log (Linux ~/.cache), only for HTML under the projects folder (FUNDUS_EXHIBIT_FORCE=1:
  anywhere); FUNDUS_EXHIBIT_PY overrides the interpreter, FUNDUS_EXHIBIT_SCRIPT the generating script.
- linkedin (1080×1350), linkedin-square (1080×1080), linkedin-landscape (1200×627): fixed frame at scale 2, fit line,
  not registered.
- <width> (posters): viewport <width>×4000 at scale 1, white space at the bottom/right cropped (+40/+30 px margin), not
  registered.
The PNG defaults to the HTML's name with .png next to it. Without a PNG argument several pages share one browser.

Chromium: Playwright's own (python -m playwright install chromium-headless-shell), else an installed Chrome/Edge
(channels), or $FUNDUS_CHROMIUM (an executable). Install the extra with `pip install -e '.[render]'`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fundus import config

MODES = {"slide": ((1920, 1080), 2), "linkedin": ((1080, 1350), 2), "linkedin-square": ((1080, 1080), 2),
         "linkedin-landscape": ((1200, 627), 2)}
VIRTUAL_TIME_MS = 3000
INSTALL_HINT = ("no headless Chromium for Playwright – pip install -e '.[render]' && "
                "python -m playwright install chromium-headless-shell")


@dataclass
class Result:
    html: str
    png: str
    size: tuple
    fit: float | None = None
    dom: str | None = None
    density: str | None = None
    registration: dict | None = None
    kind: str = "slide"

    def line(self) -> str:
        """The fit line of render.sh (`print(p, im.size, f'fit={f:.2f}', warn)`)."""
        if self.fit is None:
            return f"{self.png} {self.size}"
        f = self.fit
        warn = ("WARN: content shrunk below 85%, condense" + (" the slide" if self.kind == "slide" else "")) if f < 0.85 else ""
        return f"{self.png} {self.size} fit={f:.2f} {warn}"


# ---------------------------------------------------------------- browser
def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:      # the optional extra is missing
        raise RuntimeError(INSTALL_HINT) from e
    return sync_playwright


def launch(pw, viewport: tuple = (1920, 1080), scale: float = 2):
    """Headless Chromium: $FUNDUS_CHROMIUM, Playwright's bundled headless shell, else system Chrome / Edge.
    Window size and device scale are set on the command line exactly as render.sh does (--window-size,
    --force-device-scale-factor) and pages open without Playwright's viewport emulation: Playwright's emulated scale
    rasterises text a little differently, the command-line scale gives the same pixels as render.sh."""
    args = ["--disable-gpu", "--no-sandbox", "--hide-scrollbars", f"--force-device-scale-factor={scale:g}",
            f"--window-size={viewport[0]},{viewport[1]}"]
    exe = os.environ.get("FUNDUS_CHROMIUM")
    if exe:
        return pw.chromium.launch(executable_path=exe, headless=True, args=args)
    errors = []
    for kw in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            return pw.chromium.launch(headless=True, args=args, **kw)
        except Exception as e:     # browser not installed – try the next
            errors.append(f"{kw or 'bundled'}: {str(e).splitlines()[0][:120]}")
    raise RuntimeError(INSTALL_HINT + " (" + "; ".join(errors) + ")")


def chromium_executable() -> str | None:
    """Path of Playwright's headless Chromium (for the skill's density measurement, which calls the binary with
    --dump-dom); None without Playwright or browser."""
    try:
        sync_playwright = _playwright()
        with sync_playwright() as pw:
            for attr in ("executable_path",):
                p = getattr(pw.chromium, attr, None)
                if p and os.path.exists(p):
                    return p
    except Exception:
        return None
    return None


def _cdp(page):
    try:
        return page.context.new_cdp_session(page)
    except Exception:          # not Chromium / no DevTools protocol: plain Playwright waiting
        return None


def _load(page, url: str, cdp=None, budget_ms: int = VIRTUAL_TIME_MS) -> None:
    """Open the page and let its scripts run like Chromium's --virtual-time-budget: virtual time advances while no
    network fetch is pending, until the budget is used up; then the screenshot is taken. Falls back to real waiting
    (load, fonts, network idle) where the DevTools call is not available."""
    expired = {"v": False}
    # the budget starts once the navigation is committed (set before, it would run out on the empty start page and
    # then hold the page paused)
    page.goto(url, wait_until="commit", timeout=60000)
    if cdp is not None:
        try:
            cdp.on("Emulation.virtualTimeBudgetExpired", lambda _e: expired.__setitem__("v", True))
            cdp.send("Emulation.setVirtualTimePolicy", {"policy": "pauseIfNetworkFetchesPending", "budget": budget_ms})
        except Exception:
            cdp = None
    if cdp is not None:
        for _ in range(200):                     # at most ~10 s real time for 3 s virtual time
            if expired["v"]:
                break
            page.wait_for_timeout(50)
        try:
            cdp.send("Emulation.setVirtualTimePolicy", {"policy": "advance"})
        except Exception:
            pass
    page.wait_for_load_state("load", timeout=60000)
    if cdp is None:
        try:
            page.wait_for_load_state("networkidle", timeout=budget_ms)
        except Exception:
            pass
    try:
        page.evaluate("() => document.fonts ? document.fonts.ready.then(() => true) : true")
    except Exception:
        pass


def _shoot(browser, html: Path, png: Path, viewport: tuple) -> str:
    """Screenshot of the page at viewport size → png; returns the rendered DOM (like --dump-dom).
    The device scale comes from the command line (launch); the page size is pinned with a device-metrics override
    that keeps that scale (deviceScaleFactor 0): the window size alone is not reliable in a new browser context,
    and Playwright's own viewport emulation would also emulate the scale, which rasterises text differently."""
    ctx = browser.new_context(no_viewport=True)
    try:
        page = ctx.new_page()
        cdp = _cdp(page)
        if cdp is not None:
            try:
                cdp.send("Emulation.setDeviceMetricsOverride", {"width": viewport[0], "height": viewport[1],
                                                                "deviceScaleFactor": 0, "mobile": False})
            except Exception:
                pass
        else:
            page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
        _load(page, html.resolve().as_uri(), cdp)
        png.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(png), clip={"x": 0, "y": 0, "width": viewport[0], "height": viewport[1]})
        return page.content()
    finally:
        ctx.close()


def _fit(dom: str) -> float:
    m = re.search(r'data-fit="([0-9.]+)"', dom or "")
    try:
        return float(m.group(1)) if m else 1.0
    except ValueError:
        return 1.0


def _crop_white(png: Path) -> tuple:
    from PIL import Image, ImageChops
    im = Image.open(png).convert("RGB")
    bb = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
    if bb:
        im = im.crop((0, 0, min(im.width, bb[2] + 40), min(im.height, bb[3] + 30)))
    im.save(png)
    return im.size


def _size(png: Path) -> tuple:
    from PIL import Image
    with Image.open(png) as im:
        return im.size


def density_line(html: Path, dom: str) -> str | None:
    """The skill's density verdict (density.py dom), when the skill is installed."""
    from fundus import exhibits as ex
    mod = ex._density_mod()
    if mod is None:
        return None
    try:
        d = mod.parse_dom(dom)
        if d is None:
            d = mod.measure(str(html))
        return mod.line(d)
    except Exception:
        return None


# ---------------------------------------------------------------- registration (the render.sh hook)
def _parent_script() -> str | None:
    """The Python script that started this process (render.sh reads /proc/$PPID/cmdline the same way)."""
    ppid = os.getppid()
    proc = Path(f"/proc/{ppid}")
    if proc.is_dir():
        try:
            parts = (proc / "cmdline").read_bytes().split(b"\0")
            hit = next((p.decode(errors="replace") for p in parts if p.endswith(b".py")), None)
            if hit and not os.path.isabs(hit):
                hit = os.path.join(os.readlink(proc / "cwd"), hit)
            return hit
        except OSError:
            return None
    if config.platform_name() == "macos":        # no /proc: ps knows the command line (absolute paths only)
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(ppid)], capture_output=True, text=True, timeout=5)
            return next((w for w in out.stdout.split() if w.endswith(".py") and os.path.isabs(w)), None)
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def find_caller(explicit: str | None = None, in_process: bool = False) -> str | None:
    """The generating script: explicit, $FUNDUS_EXHIBIT_SCRIPT, this process's script (called as a library from the
    slide script) or the parent process's script (`fundus render` run by the slide script)."""
    cands = [explicit, os.environ.get("FUNDUS_EXHIBIT_SCRIPT")]
    if in_process and sys.argv and sys.argv[0].endswith(".py"):
        cands.append(os.path.abspath(sys.argv[0]))
    if not any(cands):
        cands.append(_parent_script())
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.realpath(c)
    return None


def hook_command(html: Path, png: Path, dom_file: str | None, caller: str | None, db: str | None = None,
                 python: str | None = None) -> list[str]:
    py = python or os.environ.get("FUNDUS_EXHIBIT_PY") or sys.executable
    cmd = [py, "-m", "fundus.cli"] + (["--db", str(db)] if db else []) + ["exhibits", "add", str(html), str(png), "--hook"]
    if dom_file:
        cmd += ["--dom", dom_file]
    if caller:
        cmd += ["--caller", caller]
    return cmd


def should_register(html: Path) -> tuple[bool, str]:
    if os.environ.get("FUNDUS_EXHIBIT_FORCE", "0") == "1":
        return True, "FUNDUS_EXHIBIT_FORCE=1"
    from fundus import exhibits as ex
    if ex.under_projects(str(html)):
        return True, "under the projects folder"
    return False, f"outside {ex.PROJECTS_ROOT} (FUNDUS_EXHIBIT_FORCE=1 registers anyway)"


def _detach_kwargs() -> dict:
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        return {"creationflags": flags}
    return {"start_new_session": True}


def register(html: Path, png: Path, dom: str | None, caller: str | None = None, db: str | None = None,
             wait: bool = False, dry_run: bool = False) -> dict:
    """Register a render like render.sh's hook: `fundus.cli exhibits add … --hook` in a detached process (output to the
    hook log), so the render is never slowed down or failed by Fundus. wait=True runs it in the foreground and returns
    its JSON line; dry_run=True only returns the command."""
    html, png = Path(os.path.realpath(html)), Path(os.path.realpath(png))
    if not png.is_file() or png.stat().st_size == 0:
        return {"registered": False, "reason": "no PNG"}
    ok, why = should_register(html)
    if not ok:
        return {"registered": False, "reason": why}
    log_dir = config.cache_dir()
    dom_file = None
    if dom and not dry_run:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fd, dom_file = tempfile.mkstemp(prefix="fundus-exhibit-dom.", dir=str(log_dir))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(dom)
        except OSError:
            dom_file = None
    cmd = hook_command(html, png, dom_file or ("<dom file>" if dom and dry_run else None), caller, db)
    if dry_run:
        return {"registered": False, "dry_run": True, "command": cmd}
    cwd = str(config.REPO_ROOT) if config.REPO_ROOT.is_dir() else None
    if wait:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if dom_file:
            Path(dom_file).unlink(missing_ok=True)      # the hook deletes it too
        line = (out.stdout or "").strip().splitlines()
        try:
            res = json.loads(line[-1]) if line else {}
        except ValueError:
            res = {"output": out.stdout[-500:]}
        return {"registered": out.returncode == 0, "result": res, "stderr": (out.stderr or "")[-500:] or None}
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "fundus-exhibit-hook.log", "a", encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **_detach_kwargs())
    except OSError as e:
        return {"registered": False, "reason": f"cannot start the hook: {e}"}
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    return {"registered": True, "background": True, "log": str(log_dir / "fundus-exhibit-hook.log")}


# ---------------------------------------------------------------- render
def _mode(mode: str | int | None) -> tuple[str, tuple, float]:
    mode = str(mode or "slide")
    if mode in MODES:
        vp, scale = MODES[mode]
        return mode, vp, scale
    if mode.isdigit():
        return "poster", (int(mode), 4000), 1
    raise ValueError(f"unknown mode {mode!r}: slide, linkedin, linkedin-square, linkedin-landscape or a width in px")


def render_many(pairs: list[tuple], mode: str | int | None = "slide", scale: float | None = None, register_: bool = True,
                caller: str | None = None, db: str | None = None, wait: bool = False, dry_run: bool = False,
                in_process: bool = True, quiet: bool = False) -> list[Result]:
    """Render [(html, png|None), …] in one browser. Registration as render.sh (slide mode only)."""
    kind, viewport, default_scale = _mode(mode)
    s = float(scale) if scale else default_scale
    sync_playwright = _playwright()
    out = []
    caller = find_caller(caller, in_process=in_process) if register_ and kind == "slide" else None
    with sync_playwright() as pw:
        browser = launch(pw, viewport, s)
        try:
            for html, png in pairs:
                html = Path(html)
                if not html.is_file():
                    raise FileNotFoundError(f"HTML not found: {html}")
                png = Path(png) if png else html.with_suffix(".png")
                dom = _shoot(browser, html, png, viewport)
                if kind == "poster":
                    out.append(Result(str(html), str(png), _crop_white(png), kind=kind))
                else:
                    out.append(Result(str(html), str(png), _size(png), fit=_fit(dom), dom=dom, kind=kind))
        finally:
            browser.close()
    for r in out:
        if kind == "slide":
            r.density = density_line(Path(r.html), r.dom or "")
            if register_:
                r.registration = register(Path(r.html), Path(r.png), r.dom, caller=caller, db=db, wait=wait,
                                          dry_run=dry_run)
        if not quiet:
            print(r.line())
            if r.density:
                print(r.density)
    return out


def render(html, png=None, mode: str | int | None = "slide", scale: float | None = None, register: bool = True,
           **kw) -> Result:
    """Library form for slide scripts: render(html, png) – the script calling it is taken as the generating script."""
    return render_many([(html, png)], mode=mode, scale=scale, register_=register, **kw)[0]


def render_sh() -> Path | None:
    """The skill's render.sh when it can run here (bash on POSIX), else None → use fundus.render."""
    if os.name == "nt" or not shutil.which("bash"):
        return None
    p = config.mck_skill_dir() / "render.sh"
    return p if p.is_file() else None


def render_command() -> str:
    """How a session renders a slide on this machine (shown in hints): render.sh where it works, else fundus render."""
    sh = render_sh()
    return f"{sh} <html> <png>" if sh else f"{sys.executable} -m fundus.cli render <html>"


def check(paths: list[str]) -> int:
    """render.sh --check: the skill's deck audit (density.py check) – needs the skill."""
    from fundus import exhibits as ex
    mod = ex._density_mod()
    if mod is None:
        print(f"the mck-exhibit skill is not installed at {ex.SKILL_DIR} – --check needs its density.py", file=sys.stderr)
        return 2
    files = []
    for p in paths:
        pp = Path(p)
        files += sorted(str(x) for x in pp.glob("*.html")) if pp.is_dir() else [str(pp)]
    if not mod.headless():          # the skill only knows a few Chromium paths: hand it Playwright's
        exe = chromium_executable()
        if exe:
            mod.CANDIDATES.insert(0, exe)
    return mod.main(["density.py", "check", *files]) or 0


def pairs_from_args(args: list[str]) -> list[tuple]:
    """['a.html', 'a.png', 'b.html'] → [('a.html', 'a.png'), ('b.html', None)] (a .png names the previous page's PNG)."""
    pairs = []
    for a in args:
        if a.lower().endswith(".png"):
            if not pairs or pairs[-1][1] is not None:
                raise ValueError(f"{a}: a PNG must follow its HTML")
            pairs[-1] = (pairs[-1][0], a)
        else:
            pairs.append((a, None))
    if not pairs:
        raise ValueError("give at least one HTML file")
    return pairs


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="fundus render", description="render slides HTML → PNG (render.sh in Python)")
    add_arguments(ap)
    return run_cli(ap.parse_args(argv))


def add_arguments(ap) -> None:
    ap.add_argument("files", nargs="+", help="HTML [PNG] [HTML [PNG] …] (PNG default: next to the HTML)")
    ap.add_argument("--mode", default="slide", help="slide (default) | linkedin | linkedin-square | linkedin-landscape | <width>")
    ap.add_argument("--scale", type=float, help="device scale (default 2; posters 1)")
    ap.add_argument("--check", action="store_true", help="deck audit without PNGs (the skill's density check)")
    ap.add_argument("--no-register", action="store_true", help="do not register the render in Fundus")
    ap.add_argument("--dry-run", action="store_true", help="render, but only print the registration command")
    ap.add_argument("--wait", action="store_true", help="register in the foreground and print the result")
    ap.add_argument("--script", help="the generating script (default: $FUNDUS_EXHIBIT_SCRIPT or the calling script)")


def run_cli(a, db: str | None = None) -> int:
    if a.check:
        return check(a.files)
    try:
        res = render_many(pairs_from_args(a.files), mode=a.mode, scale=a.scale, register_=not a.no_register,
                          caller=a.script, db=db, wait=a.wait, dry_run=a.dry_run, in_process=False)
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for r in res:
        if r.registration and (a.wait or a.dry_run):
            print(json.dumps(r.registration, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
