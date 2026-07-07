"""Hardening tests for the due-job scan (`_get_due_jobs_locked`).

Three regressions guarded here:

1. [H1] A cron job's stored next_run_at must be an actual occurrence of its
   schedule.expr. A bogus stored instant (hand-edited jobs.json, an expr
   change that bypassed update_job, corruption) must NOT fire — it is logged
   loudly, rescheduled to the expr's real next occurrence, and skipped.

2. [H2] A job holding a fresh ``fire_claim`` (stamped by this gateway's own
   dispatch, a concurrent ``hermes cron tick``, or an external fire_due) is
   not due — a second scheduler process sharing the same home can't re-fire
   a still-running one-shot. A stale claim (crashed claimer) does not block.

3. One malformed next_run_at timestamp must not halt the WHOLE scheduler:
   the bad job is repaired/skipped and every other job still fires.

These exercise the real store against temp cron storage (no mocks) per the
E2E-over-mocks discipline for file-touching code.
"""
from datetime import datetime, timedelta

import pytest

from cron.jobs import (
    create_job,
    load_jobs,
    save_jobs,
    get_job,
    get_due_jobs,
    claim_job_for_fire,
)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _force_next_run(job_id: str, value) -> None:
    """Overwrite a job's stored next_run_at directly (simulating a hand edit)."""
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["next_run_at"] = value
            break
    save_jobs(jobs)


class TestCronExprConsistency:
    """[H1] next_run_at must be an occurrence of schedule.expr to fire."""

    def test_bogus_next_run_at_is_skipped_and_rescheduled(self, tmp_cron_dir):
        """A stored instant the expr never describes must not fire; it is
        rescheduled to the expr's real next occurrence instead."""
        croniter = pytest.importorskip("croniter").croniter

        # A recent past instant whose minute does NOT satisfy the expr.
        stored = (datetime.now() - timedelta(minutes=3)).replace(
            second=0, microsecond=0
        )
        expr = f"{(stored.minute + 7) % 60} * * * *"
        job = create_job(prompt="x", schedule=expr, name="bogus-next-run")
        _force_next_run(job["id"], stored.isoformat())

        due = get_due_jobs()
        assert due == [], "bogus next_run_at must not fire"

        from cron.jobs import _ensure_aware, _hermes_now
        updated = get_job(job["id"])
        new_next = _ensure_aware(datetime.fromisoformat(updated["next_run_at"]))
        assert new_next > _hermes_now(), "must be rescheduled to the future"
        assert croniter.match(expr, new_next), "reschedule must satisfy the expr"

    def test_valid_past_occurrence_still_fires(self, tmp_cron_dir):
        """A genuine past occurrence of the expr keeps firing (no regression)."""
        pytest.importorskip("croniter")

        stored = (datetime.now() - timedelta(minutes=3)).replace(
            second=0, microsecond=0
        )
        expr = f"{stored.minute} * * * *"
        job = create_job(prompt="x", schedule=expr, name="valid-next-run")
        _force_next_run(job["id"], stored.isoformat())

        due = get_due_jobs()
        assert [j["id"] for j in due] == [job["id"]]

    def test_interval_jobs_are_not_expr_checked(self, tmp_cron_dir):
        """Interval jobs have no expr — an arbitrary due instant still fires."""
        job = create_job(prompt="x", schedule="every 1h", name="interval-ok")
        _force_next_run(
            job["id"], (datetime.now() - timedelta(minutes=5)).isoformat()
        )

        due = get_due_jobs()
        assert [j["id"] for j in due] == [job["id"]]


class TestFireClaimBlocksDue:
    """[H2] A fresh fire_claim keeps a job out of the due list."""

    def test_fresh_claim_hides_running_oneshot(self, tmp_cron_dir):
        """Once a fire is claimed, a second scheduler's due scan skips the job
        — the one-shot double-fire window (.tick.lock released right after
        async dispatch) is closed."""
        job = create_job(prompt="x", schedule="30m", name="oneshot")
        _force_next_run(
            job["id"], (datetime.now() - timedelta(seconds=30)).isoformat()
        )
        assert len(get_due_jobs()) == 1, "sanity: due before the claim"

        assert claim_job_for_fire(job["id"]) is True
        assert get_due_jobs() == [], "claimed fire must not be due again"

    def test_stale_claim_does_not_block(self, tmp_cron_dir):
        """A claim older than the TTL (crashed claimer) doesn't wedge the job."""
        job = create_job(prompt="x", schedule="30m", name="stale-claim")
        _force_next_run(
            job["id"], (datetime.now() - timedelta(seconds=30)).isoformat()
        )

        jobs = load_jobs()
        jobs[0]["fire_claim"] = {
            "at": (datetime.now() - timedelta(minutes=10)).isoformat(),
            "by": "dead-machine:123",
        }
        save_jobs(jobs)

        due = get_due_jobs()
        assert [j["id"] for j in due] == [job["id"]]

    def test_malformed_claim_does_not_block(self, tmp_cron_dir):
        """A corrupt fire_claim stamp is treated as absent, not as fresh."""
        job = create_job(prompt="x", schedule="30m", name="bad-claim")
        _force_next_run(
            job["id"], (datetime.now() - timedelta(seconds=30)).isoformat()
        )

        jobs = load_jobs()
        jobs[0]["fire_claim"] = {"at": "not-a-timestamp", "by": "x"}
        save_jobs(jobs)

        due = get_due_jobs()
        assert [j["id"] for j in due] == [job["id"]]


class TestMalformedNextRunAt:
    """One malformed timestamp must not halt ALL jobs."""

    def test_other_jobs_still_fire(self, tmp_cron_dir):
        """The malformed job is skipped; the healthy job after it still fires."""
        bad = create_job(prompt="x", schedule="every 1h", name="bad-ts")
        good = create_job(prompt="x", schedule="every 1h", name="good-ts")
        _force_next_run(bad["id"], "not-a-timestamp")
        _force_next_run(
            good["id"], (datetime.now() - timedelta(minutes=5)).isoformat()
        )

        due = get_due_jobs()  # must not raise
        assert [j["id"] for j in due] == [good["id"]]

    def test_malformed_recurring_job_is_repaired(self, tmp_cron_dir):
        """The bad value is replaced with a real future occurrence, so the
        job resumes its schedule instead of erroring every tick."""
        job = create_job(prompt="x", schedule="every 1h", name="repair-me")
        _force_next_run(job["id"], "garbage-value")

        assert get_due_jobs() == []

        from cron.jobs import _ensure_aware, _hermes_now
        updated = get_job(job["id"])
        assert updated["next_run_at"] != "garbage-value"
        repaired = _ensure_aware(datetime.fromisoformat(updated["next_run_at"]))
        assert repaired > _hermes_now()

    def test_malformed_future_oneshot_is_restored(self, tmp_cron_dir):
        """A malformed one-shot whose scheduled 'at' is still in the future
        gets its next_run_at restored from the schedule."""
        job = create_job(prompt="x", schedule="30m", name="restore-me")
        _force_next_run(job["id"], 12345)  # wrong type entirely

        assert get_due_jobs() == []
        updated = get_job(job["id"])
        assert updated["next_run_at"] is not None
        from cron.jobs import _ensure_aware, _hermes_now
        restored = _ensure_aware(datetime.fromisoformat(updated["next_run_at"]))
        assert restored > _hermes_now()

    def test_malformed_expired_oneshot_is_parked(self, tmp_cron_dir):
        """A malformed one-shot past its grace is parked (next_run_at=None)
        instead of re-logging a parse error every tick."""
        job = create_job(prompt="x", schedule="30m", name="park-me")
        jobs = load_jobs()
        jobs[0]["schedule"]["run_at"] = (
            datetime.now() - timedelta(hours=1)
        ).isoformat()
        jobs[0]["next_run_at"] = "garbage-value"
        save_jobs(jobs)

        assert get_due_jobs() == []
        assert get_job(job["id"])["next_run_at"] is None
        # Idempotent: a second scan neither fires nor resurrects the value.
        assert get_due_jobs() == []
        assert get_job(job["id"])["next_run_at"] is None
