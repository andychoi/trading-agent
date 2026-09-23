"""Regression tests for how scripts/restart.sh loads .env.local into its own
shell environment.

History: restart.sh originally never loaded .env.local at all except for one
special-cased grep pulling out PATHIEL_STATE_DIR. That grep had two bugs:

1. PATHIEL_STATE_DIR is OPTIONAL — services/trend_engine/env.py falls back to
   the repo root when unset, and .env.local.example doesn't even set it — so
   grep finding no match (exit 1) is the common case, not an error. The
   pipeline had no `|| true`, and restart.sh runs under `set -euo pipefail`,
   so a no-match silently killed the whole script before it printed anything
   (`restart.sh status` produced zero output, exit 1). Found 2026-09-23.

2. Every OTHER var this script reads with a bash default
   (PATHIEL_PORT, PATHIEL_SERVER_RATE_REFILL, PATHIEL_SERVER_RATE_CAPACITY,
   PATHIEL_STARTUP_GRACE_S, PATHIEL_META_PREWARM_TIMEOUT_S, PATHIEL_PY, ...)
   was silently ignoring .env.local entirely: the operator's actual Python
   process (which loads .env.local itself) picked the real value, but
   restart.sh's own bash-side decisions and printed messages used the
   hardcoded default instead — e.g. printing "starting ... on port 8000" for
   a server that was really about to bind 8090. Found the same day.

The fix generalizes the special case into `load_env_local()`, called early
in restart.sh, that loads every key in .env.local the same way
pathiel/server.py::_load_env_local_early() and
services/trend_engine/env.py::load() do (`os.environ.setdefault` semantics —
a real process env var always outranks the file).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESTART_SH = ROOT / "scripts" / "restart.sh"


def _extract_load_env_local_fn() -> str:
    src = RESTART_SH.read_text()
    m = re.search(r"load_env_local\(\) \{.*?\n\}\nload_env_local\n", src, re.S)
    assert m, "restart.sh no longer defines/calls load_env_local()"
    return m.group(0)


def _clean_env() -> dict:
    """The root conftest.py loads the real .env.local into os.environ before
    test collection (see tests/test_preflight_secrets.py's own note on this),
    so PATHIEL_*/AIGW_* vars are already ambiently set here. Strip them so
    each test's expectations are about what THIS test's synthetic .env.local
    produces, not whatever the developer's real .env.local happens to hold."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("PATHIEL_", "AIGW_", "OPENROUTER_", "FOO"))}


def _run_loader(tmp_path: Path, env_local_contents: str) -> subprocess.CompletedProcess:
    (tmp_path / ".env.local").write_text(env_local_contents)
    script = f"""
set -euo pipefail
ROOT={tmp_path}
{_extract_load_env_local_fn()}
env | grep -E '^(PATHIEL_|FOO|AIGW_)' | sort || true
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=_clean_env())


def test_loader_survives_an_env_local_without_pathiel_state_dir(tmp_path):
    """The exact shape that used to crash the whole script silently."""
    result = _run_loader(tmp_path, "SOME_OTHER_VAR=1\n")
    assert result.returncode == 0, (
        f"load_env_local crashed on an .env.local with no PATHIEL_STATE_DIR "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


def test_loader_picks_up_port_and_state_dir(tmp_path):
    result = _run_loader(
        tmp_path,
        "PATHIEL_PORT=8077\nPATHIEL_STATE_DIR=/custom/state\nAIGW_MODEL=glm-coding-flash\n",
    )
    assert result.returncode == 0, result.stderr
    lines = set(result.stdout.strip().splitlines())
    assert "PATHIEL_PORT=8077" in lines
    assert "PATHIEL_STATE_DIR=/custom/state" in lines
    assert "AIGW_MODEL=glm-coding-flash" in lines


def test_loader_skips_blank_lines_and_comments(tmp_path):
    result = _run_loader(
        tmp_path,
        "\n# a comment\n   \nPATHIEL_PORT=9090\n# PATHIEL_PORT=9999 (commented out)\n",
    )
    assert result.returncode == 0, result.stderr
    assert "PATHIEL_PORT=9090" in result.stdout


def test_process_env_outranks_the_file():
    """Matches os.environ.setdefault in the Python loaders this mirrors — a
    real environment variable must never be overwritten by the file."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        (tmp_path / ".env.local").write_text("PATHIEL_PORT=8077\n")
        script = f"""
set -euo pipefail
ROOT={tmp_path}
{_extract_load_env_local_fn()}
echo "PORT=$PATHIEL_PORT"
"""
        env = dict(os.environ)
        env["PATHIEL_PORT"] = "9999"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0
        assert "PORT=9999" in result.stdout


def test_restart_sh_status_runs_clean_in_a_repo_whose_env_local_lacks_state_dir(tmp_path):
    """End-to-end: `restart.sh status` itself, not just the extracted
    function, against a real repo checkout copy whose .env.local has no
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


def test_restart_sh_server_message_reflects_the_configured_port(tmp_path):
    """The bug that started this: restart.sh printed 'port 8000' while the
    server actually bound PATHIEL_PORT from .env.local. Exercise just the
    start_server() message logic, not a real server process."""
    src = RESTART_SH.read_text()
    m = re.search(r'local port="\$\{PATHIEL_PORT:-8000\}"\n\s*info "starting FastAPI server on port \$port', src)
    assert m, "restart.sh no longer builds its startup message from $PATHIEL_PORT"

    result = _run_loader(tmp_path, "PATHIEL_PORT=8077\n")
    assert "PATHIEL_PORT=8077" in result.stdout.strip().splitlines()
