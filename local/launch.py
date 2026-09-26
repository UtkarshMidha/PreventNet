"""Start the four local deployments: GP, Pharmacy, Lab sites + the coordinator dashboard.

Usage: uv run python -m local.launch [--superlink supergrid]
Ctrl+C stops everything; if any deployment dies, all are stopped (fail loudly).
"""

import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
HOST = "http://127.0.0.1"
PORTS = {"dashboard": 8100, "gp": 8101, "pharmacy": 8102, "lab": 8103}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--superlink", default="supergrid")
    args = parser.parse_args()

    urls = {role: f"{HOST}:{PORTS[role]}" for role in ("gp", "pharmacy", "lab")}
    # The mask secret is shared by the sites only, so the coordinator cannot unmask partial scores.
    site_env = {**os.environ, "PREVENTNET_FL_SECRET": secrets.token_hex(16)}
    coord_env = {k: v for k, v in os.environ.items() if k != "PREVENTNET_FL_SECRET"}

    procs = []
    for role, url in urls.items():
        peers = ",".join(u for r, u in urls.items() if r != role) if role == "gp" else ""
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "local.site", "--role", role, "--port", str(PORTS[role]), "--peers", peers],
            cwd=REPO, env=site_env))
    procs.append(subprocess.Popen(
        [sys.executable, "-m", "local.dashboard", "--port", str(PORTS["dashboard"]), "--superlink", args.superlink,
         *[f"--site={role}={url}" for role, url in urls.items()]],
        cwd=REPO, env=coord_env))

    print("\nPreventNet local deployments:")
    print(f"  Coordinator dashboard  {HOST}:{PORTS['dashboard']}")
    for role, url in urls.items():
        print(f"  {role:<9} site         {url}")
    print("Ctrl+C to stop.\n", flush=True)

    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("A deployment exited unexpectedly; stopping all.", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=5)


if __name__ == "__main__":
    main()
