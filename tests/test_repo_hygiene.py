"""Repo hygiene: runtime state must never be tracked in git.

Regression guard for the 2026-10-16 finding that state-paper.json
(executor paper state: positions, balances, signal ids) was tracked —
a `git checkout`/`reset --hard` would silently revert live runtime data.
Pattern follows the siklus-2 invariant-test philosophy: lock the bug class,
not just the instance.
"""
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent

# Runtime files that must never be committed (positions, balances,
# signal ids, secrets, per-host settings). Keep in sync with .gitignore.
RUNTIME_STATE = [
    "state.json",
    "state-paper.json",
    "state-live.json",
    "lifecycle.jsonl",
    "lifecycle-live.jsonl",
    "paper_trades.jsonl",
    "dashboard_settings.json",
    "signals.db",
    "last_run.json",
    "scanner_state.json",
    ".env",
    ".env.live",
    ".env.demo",
]


def _tracked_files() -> set | None:
    """None when REPO is not a git checkout (e.g. a timestamped draft copy
    — those are verified snapshots, not live repos)."""
    probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                           cwd=REPO, capture_output=True, text=True,
                           timeout=30)
    if probe.returncode != 0:
        return None
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, f"git ls-files failed: {out.stderr}"
    return set(out.stdout.split())


def test_no_runtime_state_tracked():
    tracked = _tracked_files()
    if tracked is None:
        import pytest
        pytest.skip("not a git checkout (draft snapshot)")
    leaked = sorted(f for f in RUNTIME_STATE if f in tracked)
    assert not leaked, f"runtime state tracked in git: {leaked}"


def test_gitignore_covers_runtime_state():
    gi = (REPO / ".gitignore").read_text().splitlines()
    missing = sorted(f for f in RUNTIME_STATE if f not in gi)
    assert not missing, f".gitignore missing entries: {missing}"
