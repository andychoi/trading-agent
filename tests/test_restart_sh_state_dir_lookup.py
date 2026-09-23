"""Regression test for the bug that made `scripts/restart.sh status` (and
every other action) print nothing and exit 1.

PATHIEL_STATE_DIR is OPTIONAL — services/trend_engine/env.py falls back to
the repo root when it's unset, and .env.local.example doesn't even set it.
So most .env.local files never define it, which means restart.sh's lookup
`grep -E '^PATHIEL_STATE_DIR=' .env.local` finding no match (grep exit 1) is
the COMMON case, not an error. Before the fix, that grep sat in a pipeline
assigned straight into a variable with no `|| true` guard, and restart.sh
runs under `set -euo pipefail` — so a no-match silently killed the script
before it printed anything at all. Found 2026-09-23 on a freshly created
.env.local that had no PATHIEL_STATE_DIR line.

This test extracts the real lookup line out of restart.sh (so it fails if
the fix regresses) and runs it in isolation rather than the whole script,
which also shells out to python/pgrep and inspects real processes.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESTART_SH = ROOT / "scripts" / "restart.sh"


def _extract_state_dir_lookup_line() -> str:
    src = RESTART_SH.read_text()
    m = re.search(r'PATHIEL_STATE_DIR="\$\(grep.*?\)"', src)
    assert m, "restart.sh no longer has the PATHIEL_STATE_DIR grep lookup"
    return m.group(0)


def test_the_lookup_line_still_has_the_pipefail_guard():
    assert "|| true" in _extract_state_dir_lookup_line()


def test_state_dir_lookup_survives_an_env_local_without_the_key(tmp_path):
    (tmp_path / ".env.local").write_text("SOME_OTHER_VAR=1\n")  # no PATHIEL_STATE_DIR

    script = f"""
set -euo pipefail
ROOT={tmp_path}
{_extract_state_dir_lookup_line()}
echo "OK:$PATHIEL_STATE_DIR"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, (
        "the PATHIEL_STATE_DIR lookup crashed under set -euo pipefail when "
        f".env.local had no such key (stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert result.stdout.strip() == "OK:"


def test_state_dir_lookup_still_reads_the_key_when_present(tmp_path):
    (tmp_path / ".env.local").write_text("PATHIEL_STATE_DIR=/custom/state\n")

    script = f"""
set -euo pipefail
ROOT={tmp_path}
{_extract_state_dir_lookup_line()}
echo "OK:$PATHIEL_STATE_DIR"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "OK:/custom/state"


def test_restart_sh_status_runs_clean_in_a_repo_whose_env_local_lacks_the_key(tmp_path):
    """End-to-end: `restart.sh status` itself, not just the extracted line,
    against a real repo checkout copy whose .env.local has no
    PATHIEL_STATE_DIR — the exact shape that triggered this in the field."""
    import shutil

    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "scripts", repo / "scripts")
    (repo / ".env.local").write_text("SOME_OTHER_VAR=1\n")
    (repo / "logs").mkdir()

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "restart.sh"), "status"],
        capture_output=True, text=True, cwd=repo,
    )
    assert result.returncode == 0, (
        f"restart.sh status failed with no PATHIEL_STATE_DIR in .env.local: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Status" in result.stdout
