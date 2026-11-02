"""A stand-in for the claude CLI, driven by env FAKE_MODE (default `fixture`).

- fixture: print the recorded haiku stream-json run and exit 0. Its `resetsAt` is moved to three
  days from now: the parser drops a reset the real clock finds implausible.
- hang: start a sleeping grandchild (its pid goes to FAKE_PID_FILE), print a partial line and
  sleep; only a process-group kill ends both.
- flood: write 3 MiB to stdout and exit 0.
- echo: print {"argv", "env"} as JSON (env keys as given at exec, from /proc/self/environ,
  so the interpreter's own locale coercion does not show) and exit 0.
- echo-token: write the OAuth token twice to stderr and exit 1.
- not-logged-in: print an is_error result line, echo the token to stderr and exit 1.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "haiku-2.1.304.jsonl"
RECORDED_RESET = "1794049200"


def _exec_env_keys() -> list[str]:
    raw = Path("/proc/self/environ").read_bytes()
    return sorted(item.split(b"=", 1)[0].decode() for item in raw.split(b"\0") if item)


def main() -> int:
    mode = os.environ.get("FAKE_MODE", "fixture")
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if mode == "fixture":
        reset = str(int(time.time()) + 3 * 86400)
        sys.stdout.write(FIXTURE.read_text().replace(RECORDED_RESET, reset))
        return 0
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(os.environ["FAKE_PID_FILE"]).write_text(str(child.pid))
        sys.stdout.write('{"type":"system"')
        sys.stdout.flush()
        time.sleep(60)
        return 0
    if mode == "flood":
        line = "x" * 1023 + "\n"
        for _ in range(3 * 1024):
            sys.stdout.write(line)
        return 0
    if mode == "echo":
        print(json.dumps({"argv": sys.argv[1:], "env": _exec_env_keys()}))
        return 0
    if mode == "echo-token":
        print(f"auth failed for {token}; token {token} rejected", file=sys.stderr)
        return 1
    if mode == "not-logged-in":
        print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
        print(f"using token {token}", file=sys.stderr)
        return 1
    print(f"unknown FAKE_MODE {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
