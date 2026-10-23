#!/usr/bin/env python3
"""Load, list or drop a state's parcel table by hand.

The dashboard does all of this automatically: a search loads the states the area falls in,
and the tables are dropped again 3 hours after last use. This script is for doing it from
the command line, with the same code (web/backend/app/loader.py and statedata.py).

    python load_cadastral.py --list                 # what is loaded, sizes, time to expiry
    python load_cadastral.py --state Goa            # load a state (does nothing if it is already loaded)
    python load_cadastral.py --state Goa --state Delhi
    python load_cadastral.py --drop Goa             # drop a state's whole table
    python load_cadastral.py --sweep                # drop whatever has expired (what a cron job would call)

State names are the folder names on the mount ("Goa", "Madhya Pradesh", "J&K", ...). The password
comes from ~/.pgpass.
"""
import argparse
import sys
import time

from pathlib import Path

LAP_HOME = Path(__file__).resolve().parents[1]  # the project root, wherever it is
sys.path.insert(0, str(LAP_HOME / "web"))
from backend.app import config, loader, statedata  # noqa: E402


def gb(n) -> str:
    return f"{(n or 0) / 1e9:.2f} GB"


def show() -> None:
    entries = statedata.registry()
    if not entries:
        print("no parcel tables are loaded")
        return
    for e in entries:
        left = e["expires_in_seconds"]
        expiry = "" if left is None else f"  drops in {max(left, 0) // 60} min"
        print(f"{e['state']:<40} {e['status']:<8} {e['rows_done'] or 0:>12,} rows  {gb(e['size_bytes']):>9}{expiry}")
    print(f"in use {gb(statedata.storage_in_use())} of the {config.STORAGE_BUDGET_GB:g} GB budget; tables are dropped "
          f"{config.STATE_TTL_SECONDS // 60} min after last use")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--state", action="append", default=[], help="load this state (repeatable)")
    ap.add_argument("--drop", action="append", default=[], help="drop this state's table (repeatable)")
    ap.add_argument("--sweep", action="store_true", help="drop expired tables")
    args = ap.parse_args()

    for folder in args.state:
        t0 = time.time()
        last = [0.0]

        def progress(**info):
            if time.time() - last[0] > 5 or info.get("status") in ("ready", "indexing"):
                last[0] = time.time()
                print(f"  {info.get('state')}: {info.get('status')} file {info.get('files_done')}/{info.get('files_total')} "
                      f"{info.get('rows_done') or 0:,}/{info.get('rows_total') or 0:,} rows", flush=True)

        print(f"loading {folder} ...", flush=True)
        table = loader.ensure_loaded(folder, progress)
        statedata.touch([folder])
        print(f"{folder} ready as cadastral.{table} ({time.time() - t0:.0f}s)")
    for folder in args.drop:
        print("dropped", statedata.drop_state(folder))
    if args.sweep:
        print("dropped expired:", statedata.sweep_expired() or "nothing")
    if args.list or not (args.state or args.drop or args.sweep):
        show()


if __name__ == "__main__":
    main()
