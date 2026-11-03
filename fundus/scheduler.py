"""In-process job scheduler: the systemd timers of deploy/ for machines without systemd (macOS, Windows).

The jobs, times and commands are those of deploy/fundus-{import,cleanup,embed,snapshot}.{timer,service}; each step runs
the same CLI entry point in a subprocess with this interpreter (sys.executable), so a crash or memory spike of a job
never takes the server down and the code is loaded fresh like under systemd:

    import    daily 03:15  (catch up)   fundus.cli import all; then exhibits sync, entities build (failures ignored)
    cleanup   daily 04:10  (catch up)   fundus.cli tidy run --actor nightly:tidy; trash purge --expired; datasets purge;
                                        then datasets locations, datasets fixity (failures ignored)
    embed     every 15 min              fundus.cli embed (low priority)
    snapshot  hourly :35   (catch up)   fundus.snapshot_job

Steps of a job run one after the other and stop at the first failing required step (systemd ExecStart semantics;
optional steps are the '-' ExecStartPost lines). Jobs never run at the same time (one worker; SQLite has one writer –
parallel nightly jobs caused "database is locked" on 1 Nov 2026); a job that is still waiting is not queued twice.

Persistence: <data dir>/scheduler.json keeps per job the last handled slot and the last run (start, end, result,
exit code). A 'catch up' job whose slot passed while the server was down runs once after start (systemd
Persistent=true); the embed job does not catch up (Persistent=false). The very first start only records the slots.
Output of the jobs goes to <data dir>/logs/scheduler.log (rotated at 5 MB).

When it runs (enabled()): FUNDUS_SCHEDULER=1/0 decides; otherwise on Linux only when the systemd units are NOT
installed (no double runs next to the timers), on macOS and Windows always. `fundus serve --scheduler on|off` forces it.
One scheduler per data folder (lock file); a second server on the same data leaves the jobs to the first.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from fundus import config

TIMERS = ("fundus-import.timer", "fundus-cleanup.timer", "fundus-embed.timer", "fundus-snapshot.timer")
LOG_MAX = 5 * 1024 * 1024
TRUE, FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


# ---------------------------------------------------------------- schedules
@dataclass(frozen=True)
class Daily:
    hour: int
    minute: int

    def prev(self, now: datetime) -> datetime:
        t = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        return t if t <= now else t - timedelta(days=1)

    def next(self, now: datetime) -> datetime:
        return self.prev(now) + timedelta(days=1)

    def __str__(self):
        return f"daily {self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True)
class Hourly:
    minute: int

    def prev(self, now: datetime) -> datetime:
        t = now.replace(minute=self.minute, second=0, microsecond=0)
        return t if t <= now else t - timedelta(hours=1)

    def next(self, now: datetime) -> datetime:
        return self.prev(now) + timedelta(hours=1)

    def __str__(self):
        return f"hourly :{self.minute:02d}"


@dataclass(frozen=True)
class Every:
    """Every N minutes on the clock (*:0/15 = :00, :15, :30, :45)."""
    minutes: int

    def prev(self, now: datetime) -> datetime:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        n = int((now - midnight).total_seconds() // (self.minutes * 60))
        return midnight + timedelta(minutes=n * self.minutes)

    def next(self, now: datetime) -> datetime:
        return self.prev(now) + timedelta(minutes=self.minutes)

    def __str__(self):
        return f"every {self.minutes} min"


@dataclass(frozen=True)
class Step:
    args: tuple                  # arguments after the interpreter, e.g. ("-m", "fundus.cli", "embed")
    optional: bool = False       # failure ignored (systemd '-' prefix)
    timeout: int = 6 * 3600


@dataclass(frozen=True)
class Job:
    name: str
    schedule: object
    steps: tuple
    catch_up: bool = True        # systemd Persistent=true
    low_priority: bool = False   # systemd Nice=10
    unit: str = ""               # the systemd timer it replaces

    def describe(self) -> dict:
        return {"name": self.name, "schedule": str(self.schedule), "catch_up": self.catch_up, "unit": self.unit,
                "steps": [" ".join(s.args) + (" (optional)" if s.optional else "") for s in self.steps]}


def _cli(*a, **kw) -> Step:
    return Step(("-m", "fundus.cli", *a), **kw)


JOBS = (
    Job("import", Daily(3, 15), (_cli("import", "all"), _cli("exhibits", "sync", optional=True),
                                 _cli("entities", "build", optional=True)), unit="fundus-import.timer"),
    Job("cleanup", Daily(4, 10), (_cli("tidy", "run", "--actor", "nightly:tidy"), _cli("trash", "purge", "--expired"),
                                  _cli("datasets", "purge"), _cli("datasets", "locations", optional=True),
                                  _cli("datasets", "fixity", optional=True)), unit="fundus-cleanup.timer"),
    Job("embed", Every(15), (_cli("embed", timeout=3600),), catch_up=False, low_priority=True, unit="fundus-embed.timer"),
    Job("snapshot", Hourly(35), (Step(("-m", "fundus.snapshot_job"), timeout=3600),), unit="fundus-snapshot.timer"),
)
JOB_BY_NAME = {j.name: j for j in JOBS}


# ---------------------------------------------------------------- enabled?
def systemd_user_dirs(env=None) -> list[Path]:
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    xdg = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return [xdg / "systemd" / "user", Path("/etc/systemd/user")]


def systemd_timers_installed(dirs=None) -> bool:
    for d in dirs if dirs is not None else systemd_user_dirs():
        try:
            if any((Path(d) / t).exists() for t in TIMERS):
                return True
        except OSError:
            continue
    return False


def enabled(force: bool | None = None, env=None, platform: str | None = None, systemd_dirs=None) -> tuple[bool, str]:
    """(on?, reason). force (from --scheduler on/off) beats $FUNDUS_SCHEDULER beats the platform default."""
    env = os.environ if env is None else env
    if force is not None:
        return force, "forced by --scheduler"
    v = str(env.get("FUNDUS_SCHEDULER", "")).strip().lower()
    if v in TRUE:
        return True, "FUNDUS_SCHEDULER=1"
    if v in FALSE:
        return False, "FUNDUS_SCHEDULER=0"
    pn = config.platform_name(platform)
    if pn == "linux":
        if systemd_timers_installed(systemd_dirs):
            return False, "systemd timers installed (they run the jobs)"
        return True, "Linux without the systemd timers"
    return True, f"default on {pn}"


# ---------------------------------------------------------------- state
def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _parse(s) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "scheduler.json"


def load_state(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class _FileLock:
    """Non-blocking exclusive lock on a file, POSIX (fcntl) and Windows (msvcrt); held while the object lives."""

    def __init__(self, path: Path):
        self.path, self.f = Path(path), None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.f = f
        return True

    def release(self) -> None:
        if self.f is not None:
            try:
                self.f.close()
            finally:
                self.f = None


# ---------------------------------------------------------------- running a step
def _popen_kwargs(low_priority: bool) -> dict:
    kw = {}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if low_priority:
            flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        kw["creationflags"] = flags
    return kw


def job_env(db_path) -> dict:
    env = dict(os.environ)
    env["FUNDUS_DB"] = str(db_path)
    env["PYTHONUNBUFFERED"] = "1"
    root = str(config.REPO_ROOT)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def run_step(step: Step, db_path, log, low_priority: bool = False, python: str | None = None) -> int:
    """Run one step to its end; output into the open log file. Returns the exit code (124 on timeout, 127 if it
    could not start)."""
    cmd = [python or sys.executable, *step.args]
    try:
        p = subprocess.Popen(cmd, cwd=str(config.REPO_ROOT), env=job_env(db_path), stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, **_popen_kwargs(low_priority))
    except OSError as e:
        log.write(f"cannot start {cmd}: {e}\n")
        return 127
    if low_priority and hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, p.pid, 10)
        except OSError:
            pass
    try:
        return p.wait(timeout=step.timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        log.write(f"timeout after {step.timeout} s: {' '.join(step.args)}\n")
        return 124


def _open_log(data_dir: Path):
    d = Path(data_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "scheduler.log"
    try:
        if p.exists() and p.stat().st_size > LOG_MAX:
            os.replace(p, p.with_suffix(".log.1"))
    except OSError:
        pass
    return open(p, "a", encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- the scheduler
@dataclass
class Scheduler:
    db_path: str
    jobs: tuple = JOBS
    clock: Callable[[], datetime] = datetime.now
    step_runner: Callable | None = None          # tests: (step, db_path, log, low_priority) -> exit code
    tick: float = 30.0
    data_dir: Path | None = None
    _q: queue.Queue = field(default_factory=queue.Queue, init=False)
    _pending: set = field(default_factory=set, init=False)
    _handled: dict = field(default_factory=dict, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _mu: threading.Lock = field(default_factory=threading.Lock, init=False)
    _threads: list = field(default_factory=list, init=False)
    _lock: _FileLock | None = field(default=None, init=False)
    running: str | None = field(default=None, init=False)

    def __post_init__(self):
        self.data_dir = Path(self.data_dir or Path(self.db_path).parent)
        self.state_file = state_path(self.data_dir)

    # state ------------------------------------------------------------
    def _update(self, name: str, **kw) -> None:
        with self._mu:
            st = load_state(self.state_file)
            st.setdefault("jobs", {}).setdefault(name, {}).update(kw)
            save_state(self.state_file, st)

    def prime(self) -> list[str]:
        """Set the handled slot of every job from the saved state; queue the 'catch up' jobs whose slot was missed.
        Returns the names queued."""
        now = self.clock()
        jobs_state = load_state(self.state_file).get("jobs", {})
        caught = []
        for j in self.jobs:
            cur = j.schedule.prev(now)
            saved = _parse((jobs_state.get(j.name) or {}).get("last_slot"))
            if j.catch_up and saved is not None and saved < cur:
                self._handled[j.name] = cur
                self._update(j.name, last_slot=_iso(cur))
                self._enqueue(j, reason="catch-up")
                caught.append(j.name)
            else:
                self._handled[j.name] = cur
                if saved is None or saved < cur:
                    self._update(j.name, last_slot=_iso(cur))
        return caught

    def due(self) -> list[str]:
        """Queue every job whose next slot has come (called by the loop each tick). Returns the names queued."""
        now = self.clock()
        out = []
        for j in self.jobs:
            cur = j.schedule.prev(now)
            if cur > self._handled.get(j.name, cur):
                self._handled[j.name] = cur
                self._update(j.name, last_slot=_iso(cur))
                if self._enqueue(j, reason="schedule"):
                    out.append(j.name)
        return out

    def _enqueue(self, job: Job, reason: str = "") -> bool:
        with self._mu:
            if job.name in self._pending:
                return False
            self._pending.add(job.name)
        self._q.put((job, reason))
        return True

    # running ------------------------------------------------------------
    def run_job(self, job: Job, reason: str = "manual") -> dict:
        """Run all steps of one job now (in this thread) and record the result."""
        start = self.clock()
        self.running = job.name
        self._update(job.name, last_start=_iso(start), running=True)
        results, ok = [], True
        runner = self.step_runner or run_step
        log = _open_log(self.data_dir)
        try:
            log.write(f"\n=== {_iso(start)} {job.name} ({reason}) ===\n")
            log.flush()
            for step in job.steps:
                t0 = time.monotonic()
                code = runner(step, self.db_path, log, job.low_priority)
                log.flush()
                results.append({"step": " ".join(step.args), "exit": code, "seconds": round(time.monotonic() - t0, 1)})
                if code != 0 and not step.optional:
                    ok = False
                    break
            log.write(f"=== {job.name}: {'success' if ok else 'failed'} ===\n")
        finally:
            log.close()
            self.running = None
        end = self.clock()
        rec = {"last_end": _iso(end), "last_result": "success" if ok else "failed", "running": False,
               "last_exit": next((r["exit"] for r in results if r["exit"] != 0), 0), "last_reason": reason,
               "last_seconds": round((end - start).total_seconds(), 1), "last_steps": results}
        self._update(job.name, **rec)
        return {"job": job.name, **rec}

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job, reason = self._q.get(timeout=1)
            except queue.Empty:
                continue
            with self._mu:
                self._pending.discard(job.name)
            try:
                self.run_job(job, reason)
            except Exception as e:          # never let one job end the scheduler
                print(f"fundus scheduler: {job.name} crashed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.tick):
            try:
                self.due()
            except Exception as e:
                print(f"fundus scheduler: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def start(self) -> bool:
        """Take the data folder's scheduler lock, catch up, start the loop and the worker. False if another process
        already schedules this data folder."""
        self._lock = _FileLock(self.data_dir / "scheduler.lock")
        if not self._lock.acquire():
            self._lock = None
            return False
        self.prime()
        for target, name in ((self._worker, "scheduler-worker"), (self._loop, "scheduler")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._lock:
            self._lock.release()


_current: Scheduler | None = None


def start(db_path) -> Scheduler | None:
    """Start the scheduler for the server (mcp_server.main). None when another process holds the data folder."""
    global _current
    s = Scheduler(str(db_path))
    if not s.start():
        print(f"fundus scheduler: another process schedules {s.data_dir} – not starting a second one", file=sys.stderr,
              flush=True)
        return None
    _current = s
    return s


def status(db_path=None, now: datetime | None = None) -> dict:
    """Enabled?, the jobs with schedule, next slot and last run (for `fundus scheduler status` and the health view)."""
    data_dir = Path(db_path).parent if db_path else config.data_dir()
    now = now or datetime.now()
    on, why = enabled()
    st = load_state(state_path(data_dir)).get("jobs", {})
    jobs = []
    for j in JOBS:
        rec = dict(st.get(j.name) or {})
        rec.pop("last_steps", None)
        jobs.append({**j.describe(), "next": _iso(j.schedule.next(now)), **rec})
    return {"enabled": on, "reason": why, "active_here": _current is not None, "state_file": str(state_path(data_dir)),
            "log": str(Path(data_dir) / "logs" / "scheduler.log"), "jobs": jobs}


def run_now(name: str, db_path) -> dict:
    """`fundus scheduler run JOB`: one job in the foreground, recorded like a scheduled run."""
    if name not in JOB_BY_NAME:
        raise ValueError(f"unknown job {name!r} – one of {', '.join(JOB_BY_NAME)}")
    return Scheduler(str(db_path)).run_job(JOB_BY_NAME[name], reason="manual")
