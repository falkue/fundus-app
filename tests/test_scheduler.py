"""In-process job scheduler (fundus/scheduler.py): schedules, on/off per platform, catch-up, job semantics, state, lock,
and that the jobs are those of the systemd units in deploy/."""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fundus import scheduler as sc

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_schedules():
    now = datetime(2026, 11, 2, 3, 14, 59)
    d = sc.Daily(3, 15)
    assert d.prev(now) == datetime(2026, 11, 1, 3, 15) and d.next(now) == datetime(2026, 11, 2, 3, 15)
    assert d.prev(datetime(2026, 11, 2, 3, 15)) == datetime(2026, 11, 2, 3, 15)
    h = sc.Hourly(35)
    assert h.prev(datetime(2026, 11, 2, 10, 34)) == datetime(2026, 11, 2, 9, 35)
    assert h.next(datetime(2026, 11, 2, 10, 35)) == datetime(2026, 11, 2, 11, 35)
    e = sc.Every(15)
    assert e.prev(datetime(2026, 11, 2, 10, 44, 59)) == datetime(2026, 11, 2, 10, 30)
    assert e.next(datetime(2026, 11, 2, 23, 50)) == datetime(2026, 11, 3, 0, 0)


def test_enabled_per_platform(tmp_path):
    units = tmp_path / "systemd" / "user"
    units.mkdir(parents=True)
    dirs = [units]
    assert sc.enabled(env={}, platform="darwin", systemd_dirs=dirs) == (True, "default on macos")
    assert sc.enabled(env={}, platform="win32", systemd_dirs=dirs)[0] is True
    assert sc.enabled(env={}, platform="linux", systemd_dirs=dirs)[0] is True           # no timers installed
    (units / "fundus-import.timer").write_text("[Timer]\n")
    on, why = sc.enabled(env={}, platform="linux", systemd_dirs=dirs)
    assert on is False and "systemd" in why                                              # no double runs
    assert sc.enabled(env={"FUNDUS_SCHEDULER": "1"}, platform="linux", systemd_dirs=dirs)[0] is True
    assert sc.enabled(env={"FUNDUS_SCHEDULER": "0"}, platform="darwin", systemd_dirs=dirs)[0] is False
    assert sc.enabled(force=False, env={"FUNDUS_SCHEDULER": "1"}, platform="darwin")[0] is False
    assert sc.enabled(force=True, env={}, platform="linux", systemd_dirs=dirs)[0] is True


def test_systemd_dirs_follow_xdg(tmp_path):
    dirs = sc.systemd_user_dirs({"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    assert dirs[0] == tmp_path / "cfg" / "systemd" / "user"


def _sched(tmp_path, now, calls=None, codes=None):
    calls = [] if calls is None else calls
    codes = codes or {}

    def runner(step, db, log, low):
        calls.append(" ".join(step.args))
        log.write(f"ran {step.args}\n")
        return codes.get(" ".join(step.args), 0)
    return sc.Scheduler(str(tmp_path / "f.sqlite"), clock=Clock(now), step_runner=runner), calls


def _drain(s):
    out = []
    while not s._q.empty():
        job, reason = s._q.get()
        s._pending.discard(job.name)
        out.append((job.name, reason))
        s.run_job(job, reason)
    return out


def test_first_start_records_slots_without_running(tmp_path):
    s, calls = _sched(tmp_path, datetime(2026, 11, 2, 12, 0))
    assert s.prime() == [] and calls == []
    st = sc.load_state(s.state_file)["jobs"]
    assert st["import"]["last_slot"] == "2026-11-02T03:15:00" and st["snapshot"]["last_slot"] == "2026-11-02T11:35:00"


def test_missed_runs_catch_up_once_after_start(tmp_path):
    s, calls = _sched(tmp_path, datetime(2026, 11, 2, 12, 0))
    s.prime()
    # server down from 12:00 until two days later 10:00: import (03:15) and cleanup (04:10) missed twice, snapshot many
    # times, embed too – the catch-up jobs run once each, embed (Persistent=false) waits for its next slot
    s2, calls2 = _sched(tmp_path, datetime(2026, 11, 4, 10, 0))
    assert sorted(s2.prime()) == ["cleanup", "import", "snapshot"]
    ran = _drain(s2)
    assert [r for _, r in ran] == ["catch-up"] * 3
    assert calls2[:3] == ["-m fundus.cli import all", "-m fundus.cli exhibits sync", "-m fundus.cli entities build"]
    assert "-m fundus.cli embed" not in calls2
    # a third start right after: nothing is missed any more
    s3, calls3 = _sched(tmp_path, datetime(2026, 11, 4, 10, 1))
    assert s3.prime() == [] and calls3 == []


def test_due_queues_each_slot_once(tmp_path):
    clock = Clock(datetime(2026, 11, 2, 10, 0, 30))
    s, calls = _sched(tmp_path, clock.t)
    s.clock = clock
    s.prime()
    assert s.due() == []
    clock.t = datetime(2026, 11, 2, 10, 15, 2)
    assert s.due() == ["embed"]
    assert s.due() == []                                       # same slot: not again
    clock.t = datetime(2026, 11, 2, 10, 35, 0)
    assert s.due() == ["snapshot"]                              # embed (10:30) still waiting: not queued twice
    assert [n for n, _ in _drain(s)] == ["embed", "snapshot"]
    clock.t = datetime(2026, 11, 3, 3, 15)
    assert "import" in s.due()


def test_job_steps_stop_at_first_required_failure(tmp_path):
    s, calls = _sched(tmp_path, datetime(2026, 11, 2, 12, 0), codes={"-m fundus.cli exhibits sync": 1})
    res = s.run_job(sc.JOB_BY_NAME["import"])
    assert res["last_result"] == "success" and len(calls) == 3        # optional step failed: ignored
    calls.clear()
    s.step_runner = lambda step, db, log, low: (calls.append(step.args[-1]), 2 if "tidy" in step.args else 0)[1]
    res = s.run_job(sc.JOB_BY_NAME["cleanup"])
    assert res["last_result"] == "failed" and res["last_exit"] == 2 and calls == ["nightly:tidy"]   # stops there
    st = sc.load_state(s.state_file)["jobs"]["cleanup"]
    assert st["last_result"] == "failed" and st["running"] is False and st["last_steps"][0]["exit"] == 2
    log = (tmp_path / "logs" / "scheduler.log").read_text()
    assert "=== cleanup: failed ===" in log


def test_run_step_runs_this_interpreter(tmp_path):
    log_file = tmp_path / "out.log"
    with open(log_file, "w") as log:
        code = sc.run_step(sc.Step(("-c", "import os,sys; print('db=' + os.environ['FUNDUS_DB']); sys.exit(3)")),
                           tmp_path / "x.sqlite", log, low_priority=True)
    assert code == 3 and f"db={tmp_path / 'x.sqlite'}" in log_file.read_text()
    with open(log_file, "a") as log:
        assert sc.run_step(sc.Step(("-c", "import time; time.sleep(5)"), timeout=1), tmp_path / "x.sqlite", log) == 124
        assert sc.run_step(sc.Step(("-c", "0")), tmp_path / "x.sqlite", log, python=str(tmp_path / "nope")) == 127


def test_one_scheduler_per_data_folder(tmp_path):
    a, _ = _sched(tmp_path, datetime(2026, 11, 2, 12, 0))
    b, _ = _sched(tmp_path, datetime(2026, 11, 2, 12, 0))
    a.tick = b.tick = 3600
    try:
        assert a.start() is True
        assert b.start() is False
    finally:
        a.stop()
        b.stop()
    c, _ = _sched(tmp_path, datetime(2026, 11, 2, 12, 0))
    c.tick = 3600
    assert c.start() is True
    c.stop()


def test_status_lists_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_SCHEDULER", "1")
    s, _ = _sched(tmp_path, datetime(2026, 11, 2, 12, 0))
    s.run_job(sc.JOB_BY_NAME["snapshot"])
    st = sc.status(tmp_path / "f.sqlite", now=datetime(2026, 11, 2, 12, 0))
    assert st["enabled"] is True
    snap = next(j for j in st["jobs"] if j["name"] == "snapshot")
    assert snap["next"] == "2026-11-02T12:35:00" and snap["last_result"] == "success" and "last_steps" not in snap


def test_run_now_rejects_unknown_job(tmp_path):
    with pytest.raises(ValueError):
        sc.run_now("nope", tmp_path / "f.sqlite")


# ---------------------------------------------------------------- same jobs as the systemd units
def _unit(name):
    return (ROOT / "deploy" / name).read_text()


def test_jobs_match_the_systemd_units():
    cal = {"fundus-import.timer": "*-*-* 03:15:00", "fundus-cleanup.timer": "*-*-* 04:10:00",
           "fundus-embed.timer": "*:0/15", "fundus-snapshot.timer": "*-*-* *:35:00"}
    sched = {"fundus-import.timer": sc.Daily(3, 15), "fundus-cleanup.timer": sc.Daily(4, 10),
             "fundus-embed.timer": sc.Every(15), "fundus-snapshot.timer": sc.Hourly(35)}
    for job in sc.JOBS:
        timer = _unit(job.unit)
        assert f"OnCalendar={cal[job.unit]}" in timer and job.schedule == sched[job.unit]
        assert ("Persistent=true" in timer) == job.catch_up
        service = _unit(job.unit.replace(".timer", ".service"))
        cmds = []
        for line in service.splitlines():
            m = re.match(r"(ExecStart|ExecStartPost)=(-?)\S+/python (.*)$", line)
            if m:
                cmds.append((m.group(3), m.group(2) == "-"))
        assert cmds == [(" ".join(s.args), s.optional) for s in job.steps], job.name
        assert ("Nice=10" in service) == job.low_priority
