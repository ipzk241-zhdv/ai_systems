"""Restricted SSH deployment entry point, installed outside the Git checkout.

The SSH key can request only `deploy <commit SHA>`. Configuration and runtime
files are never supplied by GitHub Actions or removed by git clean.
"""
import io
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time


REPO = Path("/home/ubuntu/ai_systems")
STATE = Path("/home/ubuntu/.local/state/news-bot-deploy")
SERVICE = "news-bot"


def run(args, cwd=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        return subprocess.run(args, cwd=cwd, env=env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    except subprocess.CalledProcessError as error:
        if "unittest" in args:
            label = "unit tests in " + args[args.index("-s") + 1]
        else:
            label = " ".join(str(value) for value in args[:4])
        reason = ""
        if args[:2] == ["git", "fetch"] and "non-fast-forward" in (error.stderr or ""):
            reason = " Remote history was rewritten; refresh the installed deployment handler."
        raise ValueError("Deployment step failed: " + label + " (exit " + str(error.returncode) + ")." + reason) from None


def parse_command(command):
    match = re.fullmatch(r"deploy ([0-9a-f]{40})", command)
    if not match:
        raise ValueError("Only deploy <40-character commit SHA> is accepted.")
    return match[1]


def extract_code(data, destination):
    """Extract only regular application files, never secrets, links or runtime data."""
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (path.is_absolute() or "\\" in member.name or ".." in path.parts or not path.parts or path.parts[0] != "mkr"
                    or ".env" in path.parts or ".runtime" in path.parts or "__pycache__" in path.parts):
                raise ValueError("Unexpected or private path in deployment archive.")
            if not (member.isfile() or member.isdir()):
                raise ValueError("Links and special files are not accepted in deployments.")
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    output.write(source.read())


def ensure_clean(repo):
    changed = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo)
    if changed:
        raise ValueError("Tracked VM files have local changes. Commit or save them before deployment.")
    # An untracked file must never turn into a tracked secret in a future commit.
    tracked = run(["git", "ls-files", "mkr/solution/.env", "mkr/solution/.runtime"], cwd=repo)
    if tracked:
        raise ValueError("Private configuration/runtime paths must not be tracked in Git.")


def snapshot(runtime, target):
    target.mkdir(parents=True, exist_ok=False)
    for database in runtime.glob("bot-*.sqlite3"):
        with closing(sqlite3.connect(str(database))) as source, closing(sqlite3.connect(str(target / database.name))) as backup:
            source.backup(backup)


def healthy(service, checks=8):
    # Catch crash/restart loops rather than accepting the first instant of `active`.
    pid = run(["systemctl", "show", service, "--property=MainPID", "--value"])
    if pid == "0":
        raise ValueError("Service did not start.")
    for _ in range(checks):
        time.sleep(3)
        state = run(["systemctl", "is-active", service])
        current = run(["systemctl", "show", service, "--property=MainPID", "--value"])
        if state != "active" or current != pid:
            raise ValueError("Service failed its startup stability check.")


def deploy(sha, repo=REPO, state=STATE, service=SERVICE):
    ensure_clean(repo)
    # A rewritten master may update the tracking ref. This does not touch HEAD,
    # local files or runtime data; candidate validation still happens below.
    run(["git", "fetch", "--no-tags", "origin", "+master:refs/remotes/origin/master"], cwd=repo)
    head = run(["git", "rev-parse", "refs/remotes/origin/master"], cwd=repo)
    if head != sha:
        print("Skipped: a newer commit is already on master.")
        return
    old = run(["git", "rev-parse", "HEAD"], cwd=repo)
    config = repo / "mkr/solution/.env"
    if not config.is_file():
        raise ValueError("The VM must already contain solution/.env.")
    archive = subprocess.run(["git", "archive", sha, "mkr"], cwd=repo, check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    with tempfile.TemporaryDirectory(prefix="news-bot-check-") as directory:
        staging = Path(directory)
        extract_code(archive, staging)
        for suite in ("solution", "deploy"):
            run([sys.executable, "-B", "-m", "unittest", "discover", "-s", suite,
                 "-p", "test_*.py"], cwd=staging / "mkr")
    runtime = repo / "mkr/solution/.runtime"
    runtime.mkdir(exist_ok=True)
    backups = state / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + sha[:12])
    # Stop only after all preflight checks have passed.
    run(["sudo", "-n", "systemctl", "stop", service])
    try:
        snapshot(runtime, backup)
        run(["git", "reset", "--hard", sha], cwd=repo)
        run(["sudo", "-n", "systemctl", "reset-failed", service])
        run(["sudo", "-n", "systemctl", "start", service])
        healthy(service)
    except Exception:
        run(["sudo", "-n", "systemctl", "stop", service])
        run(["git", "reset", "--hard", old], cwd=repo)
        run(["sudo", "-n", "systemctl", "reset-failed", service])
        run(["sudo", "-n", "systemctl", "start", service])
        print("Deployment failed; previous code was restored. Database snapshots are on the VM.", file=sys.stderr)
        raise
    state.mkdir(parents=True, exist_ok=True)
    (state / "last-success.json").write_text(json.dumps({"commit": sha, "previous": old,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "database_backup": str(backup)}), encoding="utf-8")
    print("Deployed " + sha + "; service is active. Configuration and runtime data preserved.")


def main():
    try:
        command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
        sha = parse_command(command)
        STATE.mkdir(parents=True, exist_ok=True)
        import fcntl
        with (STATE / "deploy.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(sha)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        # Don't echo arbitrary Git/SSH subprocess output or application logs.
        print(str(error) if isinstance(error, ValueError) else "Deployment failed: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
