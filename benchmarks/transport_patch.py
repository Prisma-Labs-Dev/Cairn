#!/usr/bin/env python3
"""Apply the identical benchmark-only Pi transport to disposable engine checkouts."""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import subprocess

EXECUTE = '''        if self.local:
            import os
            import uuid
            session = session or ("cairn-sessions/" + str(uuid.uuid4()) + ".jsonl")
            return DriverResult(argv=self._local_argv(worker, prompt, session), session=session)
'''
LOCAL = '''    def _local_argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        # Identical external benchmark transport for original and fork engines.
        import os
        import sys
        argv = ["--mode", "json", "--no-extensions", "--no-skills",
                "--no-prompt-templates", "--no-themes", "--no-context-files",
                "--tools", "read,write,edit,bash,grep,find,ls"]
        if session:
            argv.extend(["--session", session])
        argv.extend(["-p", prompt])
        return [sys.executable, os.environ["WAYLINE_BENCH_TRANSPORT"], "worker", "--", *argv]

'''


def patched(source):
    begin = source.index("        if self.local:", source.index("    def build_execute("))
    end = source.index("        env = worker.env", begin)
    source = source[:begin] + EXECUTE + source[end:]
    begin = source.index("    def _local_argv(")
    end = source.index("    def extract_session(", begin)
    return source[:begin] + LOCAL + source[end:]


def apply(root):
    path = root / "cairn/src/cairn/dispatcher/workers/adapters/pi.py"
    before = path.read_text()
    after = patched(before)
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                      fromfile="a/cairn/src/cairn/dispatcher/workers/adapters/pi.py",
                                      tofile="b/cairn/src/cairn/dispatcher/workers/adapters/pi.py"))
    path.write_text(after)
    return diff


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--diff", type=Path, required=True)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.checkout, text=True).strip()
    expected = subprocess.check_output(["git", "rev-parse", args.expected_revision], cwd=args.checkout, text=True).strip()
    if revision != expected or subprocess.check_output(["git", "status", "--porcelain"], cwd=args.checkout):
        parser.error("transport patch requires a clean disposable checkout at the frozen revision")
    args.diff.write_text(apply(args.checkout))
    print(revision)
