#!/usr/bin/env python3
"""Audit the declared vs. actual types of every attribute column in the parcel parquet files.

For each file, each column that the parquet file declares as a string is checked against
its values (a sample of row groups: the first, middle and last) to see whether the values
are really whole numbers, decimals, dates, codes with leading zeros, or text.
Writes one CSV row per file/column and prints a summary.
"""
import csv
import glob
import json
import os
import sys
import time

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = "/home/geoint/projects/lap_tool/data/mount"
OUT = "/home/geoint/projects/lap_tool/data_type_audit.csv"
# The pipeline reads this: for each parcel source file, the string columns whose values
# are all numbers. See web/backend/app/typecast.py.
COLUMN_MAP = "/home/geoint/projects/lap_tool/web/backend/column_types.json"
SKIP = {"geometry", "bbox"}

INT_RE = r"^[+-]?\d+$"
LEAD0_RE = r"^[+-]?0\d+$"
DEC_RE = r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$"
DATE_RE = r"^(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
BOOL_RE = r"^(?i:true|false|yes|no|y|n)$"


def count(mask) -> int:
    return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)


def classify(col: pa.ChunkedArray) -> dict:
    nonnull = col.drop_null()
    stripped = pc.utf8_trim_whitespace(nonnull)
    values = stripped.filter(pc.not_equal(stripped, ""))
    n = len(values)
    out = {"rows": len(col), "non_empty": n, "blank_pct": round(100 * (1 - n / len(col)), 1) if len(col) else 0}
    if n == 0:
        out.update(kind="empty", numeric_pct=0, example="")
        return out
    is_int = pc.match_substring_regex(values, INT_RE)
    is_dec = pc.match_substring_regex(values, DEC_RE)
    n_int, n_num = count(is_int), count(is_dec)
    n_lead0 = count(pc.match_substring_regex(values, LEAD0_RE))
    n_date = count(pc.match_substring_regex(values, DATE_RE))
    n_bool = count(pc.match_substring_regex(values, BOOL_RE))
    longest = int(pc.max(pc.utf8_length(values)).as_py())
    out["numeric_pct"] = round(100 * n_num / n, 1)
    out["example"] = str(values[0].as_py())[:30]
    if n_num == n and n_int == n:
        if n_lead0:
            out["kind"] = "int_with_leading_zeros"  # an identifier/code: the zeros are meaningful
        elif longest > 15:
            out["kind"] = "int_too_long"            # more digits than a double keeps exactly
        else:
            out["kind"] = "integer"
    elif n_num == n:
        out["kind"] = "decimal"
    elif n_bool == n:
        out["kind"] = "boolean"
    elif n_date == n:
        out["kind"] = "date_like"
    elif n_num / n >= 0.9:
        out["kind"] = "mostly_numeric"              # a few non-numeric values: stays text
    else:
        out["kind"] = "text"
    return out


def sample_groups(pf: pq.ParquetFile) -> list[int]:
    n = pf.metadata.num_row_groups
    return sorted({0, n // 2, n - 1})


def main() -> None:
    files = sorted(glob.glob(os.path.join(ROOT, "**", "*.parquet"), recursive=True))
    print(f"{len(files)} files", flush=True)
    t0 = time.time()
    numeric_columns: dict[str, list[str]] = {}
    left_as_text: dict[str, int] = {}
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "column", "declared", "kind", "rows_sampled", "non_empty", "blank_pct", "numeric_pct", "example"])
        for i, path in enumerate(files, 1):
            rel = os.path.relpath(path, ROOT)
            try:
                pf = pq.ParquetFile(path)
                schema = pf.schema_arrow
                names = [f.name for f in schema if f.name not in SKIP]
                strings = [f.name for f in schema if f.name not in SKIP and (pa.types.is_string(f.type) or pa.types.is_large_string(f.type))]
                for f in schema:
                    if f.name in SKIP or f.name in strings:
                        continue
                    w.writerow([rel, f.name, str(f.type), "typed_in_file", "", "", "", "", ""])
                if strings:
                    table = pf.read_row_groups(sample_groups(pf), columns=strings)
                    for name in strings:
                        c = classify(table.column(name))
                        w.writerow([rel, name, "string", c["kind"], c["rows"], c["non_empty"], c["blank_pct"], c["numeric_pct"], c["example"]])
                        if c["kind"] in ("integer", "decimal"):
                            numeric_columns.setdefault(rel, []).append(name)
                        elif c["kind"] not in ("empty", "text"):
                            left_as_text[c["kind"]] = left_as_text.get(c["kind"], 0) + 1
                print(f"[{i}/{len(files)}] {rel}: {len(names)} columns ({len(strings)} strings)  {time.time()-t0:.0f}s", flush=True)
            except Exception as e:  # keep going: report the failure
                print(f"[{i}/{len(files)}] FAILED {rel}: {e}", file=sys.stderr, flush=True)
    with open(COLUMN_MAP, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "about": "String columns whose values are all numbers, per parcel source file. Generated by src/audit_types.py; "
                "values of these columns are converted to numbers by backend/app/typecast.py. Codes with leading zeros, "
                "mostly-numeric parcel numbers and over-long digit strings are deliberately not listed (they stay text).",
                "numeric_columns": {k: sorted(v) for k, v in sorted(numeric_columns.items())},
                "left_as_text_counts": left_as_text,
            },
            fh,
            indent=1,
        )
    print("done ->", OUT, "and", COLUMN_MAP, flush=True)


if __name__ == "__main__":
    main()
