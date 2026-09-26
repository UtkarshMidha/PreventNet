#!/usr/bin/env python3
"""Pull the last PREVENTNET_RESULT line from a run log into viewer/latest.json.

Usage: python scripts/extract_result.py [logfile]   (reads stdin if omitted)
"""

import json
import sys
from pathlib import Path

PREFIX = "PREVENTNET_RESULT "


def main() -> None:
    text = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else sys.stdin.read()

    result_line = None
    for line in text.splitlines():
        if line.startswith(PREFIX):
            result_line = line[len(PREFIX):]

    if result_line is None:
        raise SystemExit("no PREVENTNET_RESULT line found in input")

    payload = json.loads(result_line)
    out_path = Path(__file__).parent.parent / "viewer" / "latest.json"
    out_path.write_text(json.dumps(payload, indent=2))

    print(f"wrote {out_path}")
    print(f"verdict: {payload['verdict']}")


if __name__ == "__main__":
    main()
