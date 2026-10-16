"""Type correction for parcel attribute values.

The source parquet files declare most attribute columns as strings (about 85% of
them), including columns that really hold numbers such as shape_area or area_ha,
so the values reach the database, the map and every download as text. This module
turns those values back into numbers. It runs on the attributes of each parcel as
results are produced, before anything is written out, so no data has to be reloaded.

Which columns are numeric comes from backend/column_types.json, written by
src/audit_types.py after checking the values of every column of every file. Two
guards keep the conversion safe:

* only columns the audit found to be 100% numeric are touched, so parcel numbers
  such as "12/3" or "72" in a column that also holds "72/5" stay text;
* a value is converted only when that is lossless: whole numbers without leading
  zeros (so codes like "05614" stay text) and at most 15 digits, or ordinary decimals.
  Anything else is left exactly as it was.

Values that are already numbers pass through unchanged, so applying this twice, or to
data loaded with proper types, does no harm.

This module is deliberately free of heavy imports so the loader scripts can use it too.
"""

import json
import math
import re
from functools import lru_cache
from pathlib import Path

COLUMN_MAP_PATH = Path(__file__).resolve().parents[1] / "column_types.json"

# Whole number: no sign other than "-", no leading zeros ("0" itself is fine).
_INT = re.compile(r"^-?(0|[1-9][0-9]*)$")
# Decimal: needs a decimal point or an exponent, so a plain "007" is never read as 7.0.
_DECIMAL = re.compile(r"^-?(([0-9]+\.[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+)$")
MAX_INT_DIGITS = 15  # beyond this a double no longer holds every digit exactly


@lru_cache(maxsize=1)
def _numeric_columns() -> dict[str, frozenset[str]]:
    try:
        data = json.loads(COLUMN_MAP_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}  # no audit yet: values are passed through unchanged
    return {source: frozenset(columns) for source, columns in data.get("numeric_columns", {}).items()}


def reload() -> None:
    """Forget the cached column map (after src/audit_types.py has been rerun)."""
    _numeric_columns.cache_clear()


def cast_number(value):
    """A numeric string as int or float; anything that cannot be converted losslessly is returned as it was."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if _INT.match(text):
        return int(text) if len(text.lstrip("-")) <= MAX_INT_DIGITS else value
    if _DECIMAL.match(text):
        number = float(text)
        return number if math.isfinite(number) else value
    return value


def numeric_columns(source: str) -> frozenset[str]:
    """Names of the columns of this source file (as stored in the `source` column, e.g.
    "Goa/Goa_Bhunaksha_Cadastrals.parquet") that hold numbers."""
    return _numeric_columns().get(source, frozenset())


def apply(source: str, attrs: dict | None) -> dict | None:
    """The attributes of one parcel with its numeric columns converted to numbers."""
    columns = _numeric_columns().get(source)
    if not columns or not attrs:
        return attrs
    # Visit only the numeric columns (a handful per file), not every attribute of the parcel.
    out = dict(attrs)
    for key in columns:
        value = out.get(key)
        if isinstance(value, str):
            out[key] = cast_number(value)
    return out
