#!/usr/bin/env python3
"""Copy the parcel data from the data folder into PostgreSQL, and keep it up to date.

This is the one script that fills the database. The web app only reads from it: nothing is loaded or deleted while
people search. Run it once to load everything, and again whenever parquet files are added to (or changed in) the data
folder; each run only does what is new:

    python src/sync_parcels.py                      # bring the database up to date with the whole data folder
    python src/sync_parcels.py --dry-run            # only show what would be loaded
    python src/sync_parcels.py --jobs 8             # load 8 files at the same time (default 2)
    python src/sync_parcels.py --state Goa          # one state folder (repeat the option for more)
    python src/sync_parcels.py --only Survey        # only files whose name contains this
    python src/sync_parcels.py --prune              # also remove files that are gone from the folder
    python src/sync_parcels.py --force              # reload files even if they look unchanged
    python src/sync_parcels.py --list               # what the database holds now

How it decides:
  * every loaded file is recorded in the table cadastral.parcel_files with its size and modification time; a file with
    the same size and time is skipped, a new file is loaded, a changed file has its old rows replaced;
  * each file is loaded in one transaction, so stopping the script (Ctrl-C, a crash, a reboot) never leaves half a
    file; run it again and it carries on with the files that are missing;
  * one table per state (cadastral.parcels_<state>); its spatial index is built once, after its new files are in;
  * for every file the script also stores where it has parcels (the layer map in the dashboard).

Settings (environment variables): PGHOST / PGPORT / PGDATABASE / PGUSER (password from ~/.pgpass) and MOUNT_ROOT, the
folder that holds the state folders (default LAP/data/mount). State folders are named like the state ("Goa",
"Madhya Pradesh", "J&K", ...). Run it on the machine that has the data and the database.
"""
import argparse
import sys
from pathlib import Path

LAP_HOME = Path(__file__).resolve().parents[1]  # the project root, wherever it is
sys.path.insert(0, str(LAP_HOME / "web"))
from backend.app import config, loader, statedata  # noqa: E402


def list_database() -> None:
    entries = statedata.catalog()
    if not entries:
        print("The database has no parcel data yet. Run this script without options to load the data folder.")
        return
    by_state: dict[str, list[dict]] = {}
    for e in entries:
        by_state.setdefault(e["state"], []).append(e)
    total = 0
    for state, files in by_state.items():
        rows = sum(f["rows"] for f in files)
        total += rows
        print(f"{state}: {len(files)} file(s), {rows:,} parcels (table cadastral.{files[0]['table_name']})")
        for f in files:
            print(f"    {f['name']}: {f['rows']:,} parcels, loaded {f['loaded_at'][:16].replace('T', ' ')}")
    print(f"Total: {total:,} parcels in {len(by_state)} state(s).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Copy the parcel parquet files into PostgreSQL and keep the database up to date.")
    ap.add_argument("--state", action="append", default=[], metavar="NAME", help="only this state folder (repeatable)")
    ap.add_argument("--only", metavar="TEXT", help="only files whose name contains TEXT")
    ap.add_argument("--jobs", type=int, default=2, help="files loaded at the same time (default 2; more is faster if the server has the cores)")
    ap.add_argument("--prune", action="store_true", help="remove from the database the files that are no longer in the folder")
    ap.add_argument("--force", action="store_true", help="reload files even if they look unchanged")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done, change nothing")
    ap.add_argument("--list", action="store_true", help="show what the database holds and exit")
    args = ap.parse_args()

    if args.list:
        list_database()
        return 0
    print(f"Data folder: {config.MOUNT_ROOT}\nDatabase: {config.DB.get('user', '(default user)')}@{config.DB['host']}:{config.DB['port']}/{config.DB.get('dbname', '(default database)')}")
    failed = loader.sync(args.state, args.only, args.force, args.prune, args.jobs, args.dry_run, say=lambda m: print(m, flush=True))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Files that were finished are kept; run the script again to carry on.")
        sys.exit(130)
