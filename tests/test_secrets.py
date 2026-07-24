#!/usr/bin/env python3
"""Tests for observer-kit run --secrets (op:// pointer possession boundary)."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

passed = failed = 0
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
ENV = os.environ.copy()
ENV["PYTHONPATH"] = os.pathsep.join([str(REPO), ENV.get("PYTHONPATH", "")])
ENV["OBSERVER_NO_LINT"] = "1"
ENV["OBSERVER_ALLOW_UNAPPROVED_FULL_RUN"] = "1"

from observer_kit.cli import load_secret_pointers, wrap_command_with_secrets  # noqa: E402


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(
        f"  {'PASS' if condition else 'FAIL'} {name}"
        + (f" - {detail}" if detail and not condition else "")
    )
    if condition:
        passed += 1
    else:
        failed += 1


def expect_exit(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except SystemExit as exc:
        return str(exc)
    return ""


print("Testing observer-kit run --secrets\n")

with tempfile.TemporaryDirectory(prefix="observer-secrets-") as tmp:
    root = Path(tmp)
    good = root / "secrets.env"
    good.write_text(
        "# pointers only\n"
        "HUBSPOT_TOKEN=op://vault/hubspot/credential\n"
        "CLAY_API_KEY=op://vault/clay/api-key\n"
        "HUBSPOT_TOKEN=op://vault/hubspot/credential\n",  # duplicate key
        encoding="utf-8",
    )
    keys = load_secret_pointers(good)
    ok(
        "load_secret_pointers accepts op:// values and dedupes keys",
        keys == ["HUBSPOT_TOKEN", "CLAY_API_KEY"],
        str(keys),
    )

    quoted = root / "quoted.env"
    quoted.write_text(
        'FOO="op://v/i/f"\nBAR=\'op://v/i/b\'\n',
        encoding="utf-8",
    )
    ok(
        "load_secret_pointers allows quoted op:// pointers",
        load_secret_pointers(quoted) == ["FOO", "BAR"],
    )

    plain = root / "plain.env"
    plain.write_text("HUBSPOT_TOKEN=sk-live-not-a-pointer\n", encoding="utf-8")
    msg = expect_exit(load_secret_pointers, plain)
    ok(
        "load_secret_pointers refuses plain secret values",
        "op://" in msg and "committable" in msg,
        msg,
    )

    empty = root / "empty.env"
    empty.write_text("# only comments\n\n", encoding="utf-8")
    msg = expect_exit(load_secret_pointers, empty)
    ok("load_secret_pointers refuses empty pointer files", "no KEY=op://" in msg, msg)

    missing = root / "missing.env"
    msg = expect_exit(load_secret_pointers, missing)
    ok("load_secret_pointers fails on missing file", "not found" in msg, msg)

    bad_line = root / "bad.env"
    bad_line.write_text("not-an-assignment\n", encoding="utf-8")
    msg = expect_exit(load_secret_pointers, bad_line)
    ok("load_secret_pointers rejects lines without KEY=", "expected KEY=" in msg, msg)

    # wrap without op on PATH
    path_no_op = os.pathsep.join(
        p for p in ENV.get("PATH", os.defpath).split(os.pathsep) if p and "op" not in p
    )
    # Safer: isolate PATH to empty dir so which("op") fails
    empty_bin = root / "empty-bin"
    empty_bin.mkdir()
    saved_path = os.environ.get("PATH")
    os.environ["PATH"] = str(empty_bin)
    try:
        msg = expect_exit(wrap_command_with_secrets, ["python3", "w.py"], good)
        ok(
            "wrap_command_with_secrets fails closed when op missing",
            "op" in msg and "PATH" in msg,
            msg,
        )
    finally:
        if saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved_path

    # Fake op: inject a value for HUBSPOT_TOKEN, then exec the rest after --
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    fake_op = fake_bin / "op"
    fake_op.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "${1:-}" != "run" ]]; then echo "fake-op: expected run" >&2; exit 2; fi\n'
        "shift\n"
        'env_file=""\n'
        'while [[ $# -gt 0 ]]; do\n'
        '  case "$1" in\n'
        '    --env-file) env_file="$2"; shift 2 ;;\n'
        '    --) shift; break ;;\n'
        '    *) shift ;;\n'
        "  esac\n"
        "done\n"
        'if [[ -z "$env_file" || ! -f "$env_file" ]]; then\n'
        '  echo "fake-op: missing --env-file" >&2; exit 2\n'
        "fi\n"
        "# Prove we received the env-file; inject a test value for the first key.\n"
        "export HUBSPOT_TOKEN=test-token-from-op\n"
        "export CLAY_API_KEY=test-clay-from-op\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    fake_op.chmod(fake_op.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    os.environ["PATH"] = str(fake_bin) + os.pathsep + (saved_path or os.defpath)
    try:
        wrapped, wrap_keys = wrap_command_with_secrets(["echo", "hi"], good)
        ok(
            "wrap_command_with_secrets prefixes op run --env-file --",
            wrap_keys == ["HUBSPOT_TOKEN", "CLAY_API_KEY"]
            and wrapped[0].endswith("/op")
            and wrapped[1:5] == ["run", "--env-file", str(good.resolve()), "--"]
            and wrapped[5:] == ["echo", "hi"],
            str(wrapped),
        )

        # End-to-end: observer-kit run --secrets with fake op
        project = root / "project"
        project.mkdir()
        state = project / ".observer"
        state.mkdir()
        (state / "runs").mkdir()
        secrets = state / "secrets.env"
        secrets.write_text(
            "HUBSPOT_TOKEN=op://vault/hubspot/credential\n"
            "CLAY_API_KEY=op://vault/clay/api-key\n",
            encoding="utf-8",
        )
        child = (
            "import os; "
            "print('TOKEN=' + (os.environ.get('HUBSPOT_TOKEN') or '')); "
            "print('CLAY=' + (os.environ.get('CLAY_API_KEY') or '')); "
            "print('MARKER=' + (os.environ.get('OBSERVER_SECRETS') or ''))"
        )
        run_env = ENV.copy()
        run_env["PATH"] = str(fake_bin) + os.pathsep + run_env.get("PATH", "")
        # Strip any real tokens from the parent so we only see op injection.
        run_env.pop("HUBSPOT_TOKEN", None)
        run_env.pop("CLAY_API_KEY", None)

        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--secrets",
                str(secrets),
                "--",
                sys.executable,
                "-B",
                "-c",
                child,
            ],
            cwd=project,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = proc.stdout + proc.stderr
        ok(
            "run --secrets wraps child and injects names-only OBSERVER_SECRETS",
            proc.returncode == 0
            and "secrets: injecting HUBSPOT_TOKEN, CLAY_API_KEY" in out
            and "TOKEN=test-token-from-op" in out
            and "CLAY=test-clay-from-op" in out
            and "MARKER=HUBSPOT_TOKEN,CLAY_API_KEY" in out,
            out[-800:],
        )

        # Without --secrets, child does not get fake tokens (and no wrap)
        proc2 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--",
                sys.executable,
                "-B",
                "-c",
                child,
            ],
            cwd=project,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out2 = proc2.stdout + proc2.stderr
        ok(
            "run without --secrets does not inject secrets",
            proc2.returncode == 0
            and "secrets: injecting" not in out2
            and "TOKEN=" in out2
            and "TOKEN=test-token-from-op" not in out2
            and "MARKER=" in out2
            and "MARKER=HUBSPOT_TOKEN" not in out2,
            out2[-800:],
        )

        # Plain secrets file via CLI exits non-zero
        bad_secrets = state / "bad-secrets.env"
        bad_secrets.write_text("HUBSPOT_TOKEN=literal-secret\n", encoding="utf-8")
        proc3 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--secrets",
                str(bad_secrets),
                "--",
                sys.executable,
                "-B",
                "-c",
                "print('should-not-run')",
            ],
            cwd=project,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out3 = proc3.stdout + proc3.stderr
        ok(
            "run --secrets with plain values exits before launching child",
            proc3.returncode != 0
            and "op://" in out3
            and "should-not-run" not in out3,
            out3[-500:],
        )
    finally:
        if saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved_path


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
