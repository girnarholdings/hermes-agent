"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.

Also isolates the cron storage paths (``_isolate_cron_paths`` below).
The global conftest points the ``HERMES_HOME`` env var at a per-test
tempdir, but ``cron/jobs.py`` and ``cron/suggestions.py`` resolve their
path constants (``JOBS_FILE``, ``CRON_DIR``, ...) at *import* time —
i.e. during collection, while the real environment is still in effect.
Any test that reaches ``save_jobs()`` without individually patching
those module attributes therefore writes to the real
``~/.hermes/cron/jobs.json``, creating live scheduled jobs on the
developer's machine. The autouse fixture repoints every resolved path
constant at the same per-test tempdir the env var points to, so no cron
test can touch real state regardless of what it calls.
"""

import pytest


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _isolate_cron_paths(tmp_path, monkeypatch):
    """Repoint import-time-resolved cron path constants at a per-test tempdir.

    Same directory layout the global ``_hermetic_environment`` fixture
    builds for ``HERMES_HOME`` (``tmp_path / "hermes_test"``), so code
    that re-reads the env var at call time and code that uses the
    module-level constants agree on where cron state lives.
    """
    from cron import jobs, suggestions

    hermes_dir = (tmp_path / "hermes_test").resolve()
    cron_dir = hermes_dir / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)

    # Belt and suspenders: the global conftest already sets this, but the
    # constants below must never disagree with the env var.
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))

    monkeypatch.setattr(jobs, "HERMES_DIR", hermes_dir)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(jobs, "TICKER_HEARTBEAT_FILE", cron_dir / "ticker_heartbeat")
    monkeypatch.setattr(jobs, "TICKER_SUCCESS_FILE", cron_dir / "ticker_last_success")

    monkeypatch.setattr(suggestions, "CRON_DIR", cron_dir)
    monkeypatch.setattr(suggestions, "SUGGESTIONS_FILE", cron_dir / "suggestions.json")

    yield
