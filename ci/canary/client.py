#!/usr/bin/env python3
"""CI side of the canary (docs/CANARY.md): ask the canary service to test this
job's commit, stream its output to the job log and canary-logs/canary.log,
and exit non-zero unless the last line is `CANARY-RESULT: PASS`.

Standard library only (runs in the plain python image).
"""

import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("CANARY_URL", "http://172.28.0.1:8743/run")


def main():
    os.makedirs("canary-logs", exist_ok=True)
    req = urllib.request.Request(URL, data=b"", method="POST",
                                 headers={"JOB-TOKEN": os.environ["CI_JOB_TOKEN"]})
    last = ""
    try:
        # A GitLab canary takes up to ~30 min; the job timeout bounds this.
        with urllib.request.urlopen(req, timeout=3600) as r, \
                open("canary-logs/canary.log", "w") as log:
            for raw in r:
                line = raw.decode(errors="replace")
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                if line.strip():
                    last = line.strip()
    except urllib.error.HTTPError as e:
        print(f"canary service refused: {e.code} {e.reason}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"canary service unreachable at {URL}: {e}", file=sys.stderr)
        return 1
    if last.startswith("CANARY-RESULT: PASS"):
        return 0
    if not last.startswith("CANARY-RESULT:"):
        print("canary: the stream ended without a result", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
