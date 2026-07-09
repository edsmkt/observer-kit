from __future__ import annotations

import argparse
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = Path(os.environ.get("OBSERVER_KIT_SKILL_DIR", ROOT / "skills" / "observer-kit"))


def skill_file(name: str) -> Path:
    path = SKILL_DIR / name
    if not path.exists():
        raise SystemExit(
            f"Observer Kit file not found: {path}\n"
            "Set OBSERVER_KIT_SKILL_DIR to the directory containing runguard.py "
            "and run_dashboard.py."
        )
    return path


def copy_file(src: Path, dst: Path, force: bool) -> str:
    if dst.exists() and not force:
        return f"skip existing {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return f"wrote {dst}"


def cmd_init(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    messages = [
        copy_file(skill_file("runguard.py"), project / "runguard.py", args.force),
    ]
    state_dir = (project / args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    messages.append(f"ready {state_dir}")
    if args.explain:
        messages.append(copy_file(skill_file("EXPLAIN.md"), state_dir / "EXPLAIN.md", args.force))
    if args.gitignore:
        gitignore = state_dir / ".gitignore"
        if not gitignore.exists() or args.force:
            gitignore.write_text("*.lock\nchat.jsonl\n", encoding="utf-8")
            messages.append(f"wrote {gitignore}")
    print("\n".join(messages))
    print()
    print("Next:")
    print(f"  observer-kit dashboard {state_dir}")
    print("  python3 your_workflow.py --dry-run --limit 10")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    script = skill_file("run_dashboard.py")
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(script), str(state_dir), "--port", str(args.port)]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except KeyboardInterrupt:
            print()
            return 130
    finally:
        sys.argv = old_argv
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    script = skill_file("test_runguard.py")
    cmd = [sys.executable, str(script), str(SKILL_DIR)]
    return subprocess.call(cmd)


def cmd_doctor(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    checks = []
    checks.append(("project exists", project.exists()))
    checks.append(("runguard.py vendored", (project / "runguard.py").exists()))
    checks.append(("state dir exists", (project / args.state_dir).exists()))
    checks.append(("dashboard available", skill_file("run_dashboard.py").exists()))
    checks.append(("tests available", skill_file("test_runguard.py").exists()))

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(f"{'OK ' if passed else 'ERR'} {label}")
    if not ok:
        print()
        print(f"Run: observer-kit init {project}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="observer-kit",
        description="Initialize and run Observer Kit guardrails for risky batch scripts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="vendor runguard.py and create a state dir")
    init.add_argument("project", nargs="?", default=".", help="target project directory")
    init.add_argument("--state-dir", default=".runguard", help="state directory inside the project")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.add_argument("--no-explain", dest="explain", action="store_false",
                      help="do not copy EXPLAIN.md into the state dir")
    init.add_argument("--no-gitignore", dest="gitignore", action="store_false",
                      help="do not write a state-dir .gitignore")
    init.set_defaults(func=cmd_init, explain=True, gitignore=True)

    dash = sub.add_parser("dashboard", help="run the localhost dashboard")
    dash.add_argument("state_dir", nargs="?", default=".runguard", help="ledger/state directory")
    dash.add_argument("--port", type=int, default=8484, help="dashboard port")
    dash.set_defaults(func=cmd_dashboard)

    test = sub.add_parser("test", help="run runguard acceptance tests")
    test.set_defaults(func=cmd_test)

    doctor = sub.add_parser("doctor", help="check a project for Observer Kit basics")
    doctor.add_argument("project", nargs="?", default=".", help="project directory")
    doctor.add_argument("--state-dir", default=".runguard", help="state directory inside the project")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
