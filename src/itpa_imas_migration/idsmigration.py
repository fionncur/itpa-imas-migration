#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""idsmigration -- convert tabular experimental data (CSV) into IMAS IDS objects.

Driven by a crosswalk spreadsheet that maps source CSV columns to IDS paths and
transforms. The pipeline (see `main`): load the crosswalk and dataset, validate,
build a per-row write spec, build one set of IDSs per pulse in memory, then write
the pulses to HDF5.

See docs/migration.md for the crosswalk format and full behaviour reference.
"""

from typing import Any, NamedTuple
import argparse
import ast
import builtins
import collections
import re
import logging
import sys
import time
import pathlib
from datetime import datetime, timedelta
import imas
import pandas as pd
import numpy as np
import yaml

from rich_argparse import RichHelpFormatter

# SimDB is an optional dependency: the --simdb step is disabled if it is not importable.
try:
    from simdb.config.config import Config
    from simdb.database import get_local_db, DatabaseError
    from simdb.cli.manifest import Manifest
    from simdb.database.models import Simulation

    SIMDB_AVAILABLE = True
except ImportError:
    SIMDB_AVAILABLE = False

logging.getLogger("imas").setLevel(logging.WARNING)  # Avoid IMAS errors for type mismatches.


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    """Walk up from start to the repo root (the dir holding resources/ or pyproject.toml)."""
    for candidate in (start, *start.parents):
        if (candidate / "resources").is_dir() or (candidate / "pyproject.toml").is_file():
            return candidate
    return start


HERE = pathlib.Path(__file__).resolve().parent
ROOT = _find_repo_root(HERE)

# Type aliases for the structures passed between the helpers below.
IDS = Any  # an IMAS top-level IDS object (summary, equilibrium, ...)
Branch = list[str]  # ordered node segments, e.g. ["divertor(0)", "value"]
Write = tuple[Branch, Any]  # a (branch, value) pair destined for one leaf
Descriptor = tuple[Branch, Any]  # a (leaf_segments, value) pair written alongside the value in the pulse IDS


class WriteContext(NamedTuple):
    """Which time-slice of which pulse a write belongs to, and how constant conflicts are resolved."""

    slice_index: int = 0
    n_slices: int = 1
    pulse: str | None = None
    resolve_spec: dict[str, dict] | None = None
    label: str | None = None


def is_number(x: Any) -> bool:
    """True for a real int/float, excluding bool (an int subclass)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _as_number(value: Any) -> float | None:
    """Convert a source value to a float for a numeric leaf; None if it is not a real number.

    Accepts python/numpy numbers and numeric strings; rejects bools, non-numeric strings (e.g. a
    '-' marker), None and NaN.
    """
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(num) else num


# ---------------------------------------------------------------------------
# Path / segment parsing
# ---------------------------------------------------------------------------


def parse_seg(seg: str) -> tuple[str, int]:
    """Parse a node segment, for fixed AoS indexing or placeholder (wildcard) indexes."""
    m = re.fullmatch(r"(\w+)\((\d+)\)", seg)  # Explicit index, like divertor(3)
    if m:
        return m.group(1), int(m.group(2))  # "divertor(3)" -> ("divertor", 3)
    m = re.fullmatch(r"(\w+)\(:\)", seg)  # Variable/wildcard index, like divertor(:).
    if m:
        return m.group(1), 0  # "divertor(:)" -> ("divertor", 0)
    return seg, 0  # bare segment, like "divertor" -> ("divertor", 0)


def replace_wildcard_index(branch: Branch, idx: int) -> Branch:
    """Replace wildcard index segments in a branch with a concrete index idx."""
    return [f"{seg[:-3]}({idx})" if seg.endswith("(:)") else seg for seg in branch]


def parse_source_pair(source_fields_val: Any, csv_column: str) -> tuple[str, str]:
    """Parse a source_fields cell into a (value_leaf, source_leaf) pair.

    Blank / NaN -> ("value", "source"), as a default.
    """
    if not isinstance(source_fields_val, str) or source_fields_val.strip() == "":
        return ("value", "source")
    try:
        parsed = ast.literal_eval(source_fields_val.strip())
    except (ValueError, SyntaxError) as e:
        raise ValueError(
            f"source_fields for csv_column '{csv_column}' {source_fields_val!r} is not a valid Python literal: {e}"
        )
    if not isinstance(parsed, tuple) or len(parsed) != 2 or not all(isinstance(p, str) for p in parsed):
        raise ValueError(
            f"source_fields for csv_column '{csv_column}' must be a 2-tuple of "
            f"strings, e.g. ('name', 'description'); got {parsed!r}"
        )
    return parsed


def check_errors(spec_by_machine: Any, name: str) -> dict:
    """Validate one sidecar `errors` entry: {machine: spec} for a single variable.

    Each spec is a relative float (error = |value| * rel), a 2-element range [min, max]
    (relative; the conservative max is used), or an absolute {"abs": value} written
    verbatim in IDS units. See docs/migration.md.
    """
    if not isinstance(spec_by_machine, dict):
        raise ValueError(f"errors: {name!r} must be a machine mapping, e.g. {{JET: 0.05}}; got {spec_by_machine!r}")
    for machine, spec in spec_by_machine.items():
        valid = (
            is_number(spec)
            or (isinstance(spec, (list, tuple)) and len(spec) == 2 and all(is_number(p) for p in spec))
            or (isinstance(spec, dict) and set(spec) == {"abs"} and is_number(spec["abs"]))
        )
        if not valid:
            raise ValueError(
                f"errors: {name!r} spec for machine '{machine}' must be a relative float, a 2-element "
                f"numeric range [min, max], or an absolute {{abs: value}}; got {spec!r}"
            )
    return spec_by_machine


def check_sentinels(values: Any, name: str) -> list:
    """Validate one sidecar `sentinels` entry: a list of no-data placeholders for a single variable.

    A source value exactly equal to any entry is treated as missing, so the target leaf falls back to
    the IMAS empty. Strings are stripped to match load_dataset's stripping.
    """
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"sentinels: {name!r} must be a list, e.g. [-9.999e-09, 'N/A']; got {values!r}")
    out = []
    for v in values:
        if is_number(v):
            out.append(v)
        elif isinstance(v, str):
            out.append(v.strip())
        else:
            raise ValueError(f"sentinels: {name!r} entries must be numbers or strings; got {v!r}")
    return out


def check_standard_names(value: Any, name: str) -> str:
    """Validate one sidecar `standard_names` entry.

    Used as `identifier/name` for `status=manifest` rows -- see `temp_var_name`.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"standard_names: {name!r} must be a non-empty string; got {value!r}")
    return value.strip()


DATABASE_STR_FIELDS = ("name", "version", "definitions", "paper")
DATABASE_LIST_FIELDS = ("csv", "authors", "maintainers", "previous_maintainers")


def check_database(value: Any) -> dict:
    """Validate the sidecar's `database` entry: a free-text description of the source database.

    String fields: name, version, definitions (link to the variable-definitions sheet), paper
    (citation). List-of-string fields: csv (source location), authors, maintainers,
    previous_maintainers. Every field is optional. See `format_database_comment`.
    """
    if not isinstance(value, dict):
        raise ValueError(f"database: must be a mapping of description fields; got {value!r}")
    for key, entry in value.items():
        if key in DATABASE_LIST_FIELDS:
            if not isinstance(entry, (list, tuple)) or not all(isinstance(e, str) for e in entry):
                raise ValueError(f"database: {key!r} must be a list of strings; got {entry!r}")
        elif key in DATABASE_STR_FIELDS:
            if not isinstance(entry, str):
                raise ValueError(f"database: {key!r} must be a string; got {entry!r}")
        else:
            raise ValueError(
                f"database: unknown field {key!r}; expected one of {DATABASE_STR_FIELDS + DATABASE_LIST_FIELDS}"
            )
    return value


def format_database_comment(database: dict) -> str | None:
    """Render the sidecar's `database` entry into one string for `ids_properties.comment`.

    None if the section is absent or empty, so no comment is written.
    """
    if not database:
        return None
    parts = []
    header = database.get("name", "")
    if header and database.get("version"):
        header += f" ({database['version']})"
    if header:
        parts.append(header)
    if database.get("definitions"):
        parts.append(f"Variable definitions: {database['definitions']}")
    if database.get("paper"):
        parts.append(f"Reference: {database['paper']}")
    if database.get("csv"):
        parts.append(f"Source data: {', '.join(database['csv'])}")
    if database.get("authors"):
        parts.append(f"Authors: {', '.join(database['authors'])}")
    if database.get("maintainers"):
        parts.append(f"Maintainers: {', '.join(database['maintainers'])}")
    if database.get("previous_maintainers"):
        parts.append(f"Previous maintainers: {', '.join(database['previous_maintainers'])}")
    return " -- ".join(parts) if parts else None


def _try_parse_dict(x: Any) -> Any:
    """If x is a string that looks like a dict literal, parse and return the dict; else return x."""
    if isinstance(x, str) and x.strip().startswith("{"):
        try:
            parsed = ast.literal_eval(x.strip())
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, SyntaxError):
            pass
    return x


def error_bar(spec: Any, value: Any) -> Any:
    """Resolve a per-machine error spec + leaf value into an absolute _error_upper magnitude.

    - relative float    -> |value| * spec
    - range [min, max]   -> |value| * max (conservative upper bound)
    - {"abs": v}         -> v verbatim, in IDS units, independent of value
    """
    if isinstance(spec, (list, tuple)):
        return np.abs(value) * max(spec)
    if isinstance(spec, dict):  # {"abs": v}
        a = float(spec["abs"])
        return np.full(value.shape, a) if isinstance(value, np.ndarray) else a
    return np.abs(value) * spec  # relative float


# ---------------------------------------------------------------------------
# IDS construction & writing
# ---------------------------------------------------------------------------


def new_ids(factory: imas.IDSFactory, name: str, comment: str | None = None) -> IDS:
    """Create a fresh top-level IDS with homogeneous time mode set and an optional source comment.

    `comment` (from the sidecar's `database` section, see `format_database_comment`) is written
    verbatim to `ids_properties.comment` when given.
    """
    ids = factory.new(name)
    ids.ids_properties.homogeneous_time = imas.ids_defs.IDS_TIME_MODE_HOMOGENEOUS
    if comment:
        ids.ids_properties.comment = comment
    return ids


def resolve_parent(ids: IDS, branch: Branch) -> tuple[IDS, str]:
    """Navigate to the parent of the branch leaf, resizing any struct-arrays as needed."""
    node = ids
    for seg in branch[:-1]:
        attr, idx = parse_seg(seg)
        node = getattr(node, attr)
        if isinstance(node, imas.ids_struct_array.IDSStructArray):
            if len(node) <= idx:
                node.resize(idx + 1, keep=True)
            node = node[idx]
    return node, parse_seg(branch[-1])[0]


def _values_equal(a: Any, b: Any) -> bool:
    """Loose equality used for the constant-consistency check across a pulse's time-slices."""
    try:
        if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
            return np.array_equal(np.asarray(a), np.asarray(b), equal_nan=True)
        if is_number(a) and is_number(b):
            return bool(np.isclose(a, b, equal_nan=True))
        return a == b
    except Exception:
        return False


KNOWN_RESOLVE_STRATEGIES = {"keep_first", "keep_last", "max", "min", "avoid"}


def _resolve_conflict(strategy: str, old: Any, new: Any, avoid: list | None = None) -> Any:
    """Reduce a pair of conflicting constant-leaf values to one, per a named strategy."""
    if strategy == "keep_first":
        return old
    if strategy == "keep_last":
        return new
    if strategy == "max":
        return max(old, new)
    if strategy == "min":
        return min(old, new)
    if strategy == "avoid":
        avoid = avoid or []
        old_bad, new_bad = old in avoid, new in avoid
        if old_bad and not new_bad:
            return new
        return old
    raise ValueError(f"unknown resolve strategy {strategy!r}")


VERBOSE = False  # --verbose: report each constant conflict inline, not just the closing tally
_seen_const_mismatch: set[tuple[Any, str]] = set()  # (pulse context, leaf path) already counted, at most once each
_conflict_counts: collections.Counter = collections.Counter()  # (leaf path, strategy label) -> pulses resolved


def report_conflict_summary() -> None:
    """Print a one-line-per-(variable, strategy) tally of constant-conflict resolutions."""
    if not _conflict_counts:
        return
    name_width = max(len(name) for name, _ in _conflict_counts)
    strategy_width = max(len(strategy) for _, strategy in _conflict_counts)
    lines = [
        f"  {name:{name_width}}  {strategy:{strategy_width}}  {count} pulse(s)"
        for (name, strategy), count in sorted(_conflict_counts.items())
    ]
    _print_over_progress("Constant conflicts resolved across slices:\n" + "\n".join(lines))


def set_slice(parent: IDS, leaf: str, target: Any, value: Any, context: WriteContext) -> None:
    """Place `value` at `context.slice_index`, growing the leaf to `context.n_slices` with appropriate padding.

    Numeric leaves use IMAS empty placeholders; string leaves pad with "".
    """
    n_slices = context.n_slices
    if target.metadata.data_type.name == "STR":
        cur = list(target.value) if target.has_value else []
        if len(cur) < n_slices:
            cur += [""] * (n_slices - len(cur))
    else:
        is_int = target.metadata.data_type.name == "INT"
        dtype = np.int32 if is_int else np.float64
        cur = np.atleast_1d(np.array(target.value, copy=True)) if target.has_value else np.empty(0, dtype=dtype)
        if cur.size < n_slices:
            empty = imas.ids_defs.EMPTY_INT if is_int else imas.ids_defs.EMPTY_FLOAT
            grown = np.full(n_slices, empty, dtype=dtype)
            if cur.size:
                grown[: cur.size] = cur
            cur = grown
    cur[context.slice_index] = value
    setattr(parent, leaf, cur)


def set_leaf(ids: IDS, branch: Branch, value: Any, context: WriteContext = WriteContext()) -> None:
    """Write `value` to the leaf at `branch` for time-slice `context.slice_index` of the pulse.

    Dynamic numeric leaves are filled by array position; constant/static leaves are written once
    and, when `context.n_slices > 1`, checked for agreement across slices. On mismatch,
    `context.resolve_spec` (variable name -> {strategy, ...}, see `load_sidecar`) is consulted to
    reduce the conflicting values; unconfigured variables keep a default of warning and keeping first.
    """
    parent, leaf = resolve_parent(ids, branch)
    target = getattr(parent, leaf)

    # Type gate: a value that does not match its leaf's dtype is treated as missing, so the leaf keeps
    # its IMAS empty.
    dtype_name = getattr(getattr(target.metadata, "data_type", None), "name", None)
    if dtype_name in ("INT", "FLT"):
        num = _as_number(value)
        if num is None:
            return
        value = int(num) if dtype_name == "INT" else num
    elif dtype_name == "STR" and isinstance(value, str) and not any(c.isalnum() for c in value):
        return

    if isinstance(target, imas.ids_struct_array.IDSStructArray):
        if target.metadata.type.is_dynamic:
            if len(target) <= context.slice_index:
                target.resize(context.slice_index + 1, keep=True)
            target[context.slice_index] = value
        else:
            if len(target) == 0:
                target.resize(1)
            target[0] = value
    elif isinstance(target, imas.ids_primitive.IDSNumericArray):
        if target.metadata.type.is_dynamic:
            set_slice(parent, leaf, target, value, context)
        else:
            setattr(parent, leaf, np.atleast_1d(value))
    elif target.metadata.data_type.name == "STR" and target.metadata.ndim >= 1:
        set_slice(parent, leaf, target, value, context)
    else:
        # Scalar / string leaf: constant or static. Write once; on disagreement across slices,
        # resolve via `context.resolve_spec` if the variable is configured, else keep first. Every
        # occurrence is tallied.
        if context.n_slices > 1 and target.has_value:
            # Identify the quantity by its crosswalk variable name
            name = context.label or "/".join(branch)
            previous = target.value
            if not _values_equal(previous, value):
                spec = (context.resolve_spec or {}).get(name)
                strategy = spec["strategy"] if spec else "default (keep_first)"
                first_for_pulse = (context.pulse, name) not in _seen_const_mismatch
                if first_for_pulse:
                    _seen_const_mismatch.add((context.pulse, name))
                    _conflict_counts[(name, strategy)] += 1
                kept = previous
                if spec is not None:
                    kept = _resolve_conflict(strategy, previous, value, spec.get("avoid"))
                    if not _values_equal(kept, previous):
                        setattr(parent, leaf, kept)
                if VERBOSE and first_for_pulse:
                    _print_over_progress(
                        f"  conflict  {context.pulse}  {name}: {previous!r} vs {value!r} "
                        f"-- {strategy} keeps {kept!r}"
                    )
            return  # keep the first value (or the value set by the resolved strategy above)
        setattr(parent, leaf, value)


def write_values(ids: IDS, writes: list[Write], context: WriteContext = WriteContext()) -> None:
    """Write resolved (branch, value) pairs to their leaves in the pulse `ids`."""
    for branch, value in writes:
        set_leaf(ids, branch, value, context)


def write_descriptors(
    ids: IDS,
    writes: list[Write],
    descriptors: list[Descriptor],
    value_leaf_depth: int,
    context: WriteContext = WriteContext(),
) -> None:
    """Write each descriptor (leaf_segments, value) into the pulse IDS alongside values.

    Each is written at the sibling of every value's parent node (branch[:-1]), so it
    lands at the correct path for each expanded AoS slot -- e.g. the source sibling, or
    a manifest row's identifier name/description.
    """
    for branch, _ in writes:
        anchor = branch[: len(branch) - value_leaf_depth]
        for desc_leaf, desc_val in descriptors:
            set_leaf(ids, anchor + list(desc_leaf), desc_val, context)


def backfill_time(ids: IDS, times: Any = None) -> None:
    """Set the root `time` if it is still empty, so dynamic nodes have a coordinate.

    Per-row mode passes nothing (a single NaN-equivalent [0.0] column); the per-pulse driver
    passes the pulse's ordered time vector for any root not already populated via a TIME mapping.
    """
    if ids.ids_properties.homogeneous_time == imas.ids_defs.IDS_TIME_MODE_HOMOGENEOUS and len(ids.time) == 0:
        ids.time = np.asarray([0.0] if times is None else times, dtype=float)


# ---------------------------------------------------------------------------
# Transform resolution
# ---------------------------------------------------------------------------


def resolve_writes(ids_branch: Branch, value: Any, cw_row: pd.Series, data_row: pd.Series) -> list[Write]:
    """Compute the (branch, value) writes a transform produces.

    Returns [] to skip the row (e.g. dictionary miss).
    """
    transform = cw_row["transform"]
    if transform == "identity":
        return [(ids_branch, value)]
    if transform == "dictionary":
        if not isinstance(cw_row["transform_args"], str):
            raise ValueError(f"Row {cw_row.name}: transform='dictionary' but transform_args is missing")
        dictionary = ast.literal_eval(cw_row["transform_args"])
        if value not in dictionary:
            return []  # uncovered values are reported upfront by validate()
        mapped = dictionary[value]
        if isinstance(mapped, list):  # Dictionary of lists feature, expand AoS to fit len(mapped).
            return [(replace_wildcard_index(ids_branch, i), v) for i, v in enumerate(mapped)]
        return [(replace_wildcard_index(ids_branch, 0), mapped)]
    if transform == "formula":
        if not isinstance(cw_row["transform_args"], str):
            raise ValueError(f"Row {cw_row.name}: transform='formula' but transform_args is missing")
        # Evaluate the expression with the data row's columns bound as bare variables, so a
        # formula like "TIMEX - TIMEY" resolves to data_row["TIMEX"] - data_row["TIMEY"].
        try:
            result = eval(cw_row["transform_args"], {**data_row.to_dict(), "datetime": datetime})
        except Exception as exc:
            print(f"WARNING: Row {cw_row.name}: formula {cw_row['transform_args']!r} failed: {exc} -- skipping")
            return []
        return [(ids_branch, result)]
    raise ValueError(f"Row {cw_row.name}: unhandled transform '{cw_row['transform']}'")


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


_progress_dangling = False  # last stdout write was a \r-redrawn progress line, with no trailing newline
_last_progress_width = 0  # length of that line, so the next redraw pads over any leftover characters


def _print_over_progress(msg: str) -> None:
    """Print `msg` on its own line, first breaking out of any dangling `\\r`-redrawn progress line."""
    global _progress_dangling
    if _progress_dangling:
        print()
        _progress_dangling = False
    print(msg)


def report_progress(
    count: int, total: int, label: Any, start: float, last_report: float, interval: float = 5.0
) -> float:
    """Timed, counted progress for the long pulse loops (build-in-memory and write-to-disk).

    Redraws in place (carriage return, no newline) on an interactive terminal; falls back to one
    line per update when stdout is redirected to a file/pipe, where redrawing would just garble.
    """
    global _progress_dangling, _last_progress_width
    now = time.monotonic()
    if now - last_report < interval:
        return last_report
    elapsed = now - start
    rate = count / elapsed if elapsed > 0 else 0
    eta = (total - count) / rate if rate > 0 else 0
    pct = 100 * count / total
    msg = (
        f"Progress: {count:{len(str(total))}d}/{total} ({pct:3.1f}%)  pulse {label}  "
        f"elapsed {timedelta(seconds=int(elapsed))}  "
        f"ETA {timedelta(seconds=int(eta))}  "
        f"({rate:.1f} pulses/s)"
    )
    if sys.stdout.isatty():
        print(msg.ljust(_last_progress_width), end="\r", flush=True)
        _last_progress_width = len(msg)
        _progress_dangling = True
    else:
        print(msg)
    return now


def report_summary(verb: str, count: int, total: int, start: float, suffix: str = "") -> None:
    """Closing one-line summary for a pulse loop."""
    _print_over_progress(f"{verb} {count}/{total} pulses{suffix} in {timedelta(seconds=int(time.monotonic() - start))}")


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Convert tabular experimental data (CSV) into IMAS IDS objects via a crosswalk spreadsheet",
        formatter_class=RichHelpFormatter,
    )
    parser.add_argument(
        "-e",
        "--experiment",
        type=str,
        default="2008",
        help="Sub-folder under resources/results/ for output \t(default=%(default)s)",
    )
    parser.add_argument(
        "-d",
        "--dataset",
        type=str,
        default="2008_data.csv",
        help="Input CSV filename under resources/input/ \t(default=%(default)s)",
    )
    parser.add_argument(
        "-m",
        "--mapping",
        type=str,
        default="2008_crosswalk.xlsx",
        help="Crosswalk spreadsheet filename under resources/mappings/ \t(default=%(default)s)",
    )
    parser.add_argument(
        "--dd-version",
        type=str,
        default="4.1.1",
        help="Data Dictionary version used to build the IDS factory \t(default=%(default)s)",
    )
    parser.add_argument(
        "--simdb",
        action="store_true",
        help="Ingest each migrated pulse into the local SimDB; diverts manifest quantities "
        "into manifest variables instead of a temporary IDS \t(requires the simdb package)",
    )
    parser.add_argument(
        "--per-time-slice",
        action="store_true",
        help="Group CSV rows by (machine, pulse) and write one IDS set per pulse with all its "
        "time-slices, instead of one IDS set per row",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print each constant-quantity conflict as it is resolved (pulse, variable, the two "
        "disagreeing values, strategy applied and the value kept)",
    )
    return parser.parse_args()


SIDECAR_SECTIONS = ("resolve", "sentinels", "errors", "standard_names", "database")


def load_sidecar(mapping_path: pathlib.Path) -> dict[str, dict]:
    """Load the per-dataset sidecar next to a crosswalk, as {section: {csv_column: entry}}.

    The sidecar (`<mapping stem>.yaml`) carries the per-variable data that has no place in a
    one-row-per-variable spreadsheet:

      resolve        : conflict-resolution strategy for a constant that disagrees across a pulse's
                        time-slices -- see `_resolve_conflict`
      sentinels      : no-data placeholder values -- see `check_sentinels`
      errors         : per-machine error bars -- see `check_errors`
      standard_names : IMAS standard name for a manifest variable -- see `check_standard_names`
      database       : free-text description of the source database, written into every pulse's
                        `ids_properties.comment` -- see `check_database`/`format_database_comment`

    Every section is optional, however a missing file gives warning.
    """
    spec_path = pathlib.Path(mapping_path).with_suffix(".yaml")
    if not spec_path.is_file():
        print(f"WARNING: no sidecar at {spec_path} -- no sentinels, error bars or conflict rules will be applied")
        return {section: {} for section in SIDECAR_SECTIONS}

    with open(spec_path, encoding="utf-8") as f:
        spec = yaml.safe_load(f) or {}
    unknown_sections = set(spec) - set(SIDECAR_SECTIONS)
    if unknown_sections:
        raise ValueError(
            f"{spec_path}: unknown section(s) {sorted(unknown_sections)}; expected {list(SIDECAR_SECTIONS)}"
        )

    sidecar = {section: dict(spec.get(section) or {}) for section in SIDECAR_SECTIONS}
    for name, entry in sidecar["resolve"].items():
        strategy = entry.get("strategy")
        if strategy not in KNOWN_RESOLVE_STRATEGIES:
            raise ValueError(
                f"{spec_path}: {name!r} has unknown strategy {strategy!r}; expected one of "
                f"{sorted(KNOWN_RESOLVE_STRATEGIES)}"
            )
        if strategy == "avoid" and not entry.get("avoid"):
            raise ValueError(f"{spec_path}: {name!r} uses strategy 'avoid' but has no 'avoid' list")
    sidecar["errors"] = {name: check_errors(spec, name) for name, spec in sidecar["errors"].items()}
    sidecar["sentinels"] = {name: check_sentinels(values, name) for name, values in sidecar["sentinels"].items()}
    sidecar["standard_names"] = {
        name: check_standard_names(value, name) for name, value in sidecar["standard_names"].items()
    }
    sidecar["database"] = check_database(sidecar["database"])
    return sidecar


def load_crosswalk(mapping_path: pathlib.Path, sidecar: dict[str, dict]) -> pd.DataFrame:
    """Read the crosswalk xlsx, keep implemented+accepted rows, and attach the sidecar entries."""
    df = pd.read_excel(mapping_path)
    all_csv_columns = set(df["csv_column"].dropna().astype(str))

    # Parse source cells that are dict literals (machine-specific provenance).
    df["source"] = df["source"].map(_try_parse_dict)

    # Include only implemented transforms (identity/dictionary/formula) with an accepted status.
    keep_mask = df["transform"].isin(["identity", "dictionary", "formula"]) & df["status"].isin(
        ["mapped", "manifest", "mapped_caveat"]
    )
    df = df[keep_mask]

    # Parse the optional source_fields column into (value_leaf, source_leaf) pairs. Blank/NaN
    # rows get the ("value", "source") default here and are then resolved against the Data
    # Dictionary by `resolve_value_leaves`, which decides whether they take a pair write at all.
    if "source_fields" in df.columns:
        pairs = [parse_source_pair(sf, col) for sf, col in zip(df["source_fields"], df["csv_column"])]
    else:
        pairs = [("value", "source")] * len(df)
    df["_source_pair"] = pd.Series(pairs, index=df.index, dtype=object)

    # Attach the sidecar's per-variable entries, keyed by csv_column; absent -> None.
    errors = [sidecar["errors"].get(str(name)) for name in df["csv_column"]]
    df["_errors"] = pd.Series(errors, index=df.index, dtype=object)

    sentinels = [sidecar["sentinels"].get(str(name)) for name in df["csv_column"]]
    df["_sentinels"] = pd.Series(sentinels, index=df.index, dtype=object)

    standard_names = [sidecar["standard_names"].get(str(name)) for name in df["csv_column"]]
    df["_standard_name"] = pd.Series(standard_names, index=df.index, dtype=object)

    # _check_sidecar_names matches against every variable in the crosswalk, not just the kept rows.
    df.attrs["all_csv_columns"] = all_csv_columns
    return df


def load_dataset(data_path: pathlib.Path) -> pd.DataFrame:
    """Import experimental data from csv, stripping whitespace from string cells."""
    data = pd.read_csv(data_path)
    return data.map(lambda x: x.strip() if isinstance(x, str) else x)


def _dd_node_meta(path: str, factory: imas.IDSFactory, ids_cache: dict[str, Any]) -> Any:
    """DD metadata for an imas_path (indexes stripped), or None when the path is not in the DD."""
    segments = [parse_seg(seg)[0] for seg in path.strip().split("/")]
    root, node_path = segments[0], "/".join(segments[1:])
    try:
        if root not in ids_cache:
            ids_cache[root] = factory.new(root)
        return ids_cache[root].metadata[node_path]
    except (KeyError, imas.exception.IDSNameError):
        return None


def _has_leaf(node_meta: Any, leaf: str) -> bool:
    """True when the DD node has `leaf` as a sub-field."""
    try:
        node_meta[leaf]
    except KeyError:
        return False
    return True


def resolve_value_leaves(df: pd.DataFrame, factory: imas.IDSFactory) -> None:
    """Derive each row's value leaf from the Data Dictionary.

    A `source_fields` cell overrides both leaf names and is taken as given (`_check_dd_paths` then
    verifies the named leaves exist). Rows with no usable imas_path -- `manifest` rows, whose target
    is a temporary bucket resolved in `build_write_spec` -- take no pair write.
    """
    ids_cache: dict[str, Any] = {}
    pairs, needs = [], []
    for _, row in df.iterrows():
        pair = row["_source_pair"]
        explicit = isinstance(row.get("source_fields"), str) and row["source_fields"].strip() != ""
        if not explicit:
            path = row["imas_path"]
            if row["status"] == "manifest" or not isinstance(path, str):
                pair = ("", pair[1])
            else:
                node_meta = _dd_node_meta(path.split("&")[0], factory, ids_cache)
                # An unknown path is reported by _check_dd_paths; assume the pair write it names.
                pair = (pair[0] if node_meta is None or _has_leaf(node_meta, pair[0]) else "", pair[1])
        pairs.append(pair)
        needs.append(bool(pair[0]))
    df["_source_pair"] = pd.Series(pairs, index=df.index, dtype=object)
    df["_needs_source"] = pd.Series(needs, index=df.index, dtype=bool)


def _check_dd_paths(df: pd.DataFrame, factory: imas.IDSFactory) -> None:
    """Every imas_path must exist in the DD; pair-write rows need both leaves."""
    ids_cache: dict[str, Any] = {}
    bad: list[str] = []
    for _, row in df[df["status"] != "manifest"].iterrows():
        if not isinstance(row["imas_path"], str):
            continue
        for path in row["imas_path"].split("&"):
            node_meta = _dd_node_meta(path, factory, ids_cache)
            if node_meta is None:
                bad.append(f"row {row.name} ('{row['csv_column']}'): '{path}' not in DD")
                continue
            if row["_needs_source"]:
                for leaf in row["_source_pair"]:
                    if not _has_leaf(node_meta, leaf):
                        bad.append(
                            f"row {row.name} ('{row['csv_column']}'): '{path}' has no '{leaf}' "
                            f"sub-field required by its source_fields pair"
                        )
    if bad:
        raise ValueError("imas_path validation failed:\n  " + "\n  ".join(bad))


def _check_dictionary_coverage(df: pd.DataFrame, data: pd.DataFrame) -> None:
    """Report every observed CSV value that a dictionary transform has no key for (rows will be skipped)."""
    for _, row in df[df["transform"] == "dictionary"].iterrows():
        if not isinstance(row["transform_args"], str):
            raise ValueError(f"Row {row.name}: transform='dictionary' but transform_args is missing")
        dictionary = ast.literal_eval(row["transform_args"])
        observed = data[row["csv_column"]].dropna()
        sentinels = row["_sentinels"] or []
        uncovered = observed[
            ~observed.isin(dictionary.keys()) & ~observed.eq("") & ~observed.isin(sentinels)
        ].value_counts()  # Manually filter empty string
        if len(uncovered):
            details = ", ".join(f"{v!r} x{c}" for v, c in uncovered.items())
            print(
                f"WARNING: Row {row.name}: dictionary for column '{row['csv_column']}' does not cover "
                f"observed value(s) {details} -- these rows will be skipped"
            )


def _check_formula_identifiers(df: pd.DataFrame, data: pd.DataFrame) -> None:
    """Every bare name in a formula must be a CSV column or a Python builtin."""
    allowed = set(data.columns) | set(dir(builtins)) | {"datetime"}
    for _, row in df[df["transform"] == "formula"].iterrows():
        if not isinstance(row["transform_args"], str):
            raise ValueError(f"Row {row.name}: transform='formula' but transform_args is missing")
        try:
            tree = ast.parse(row["transform_args"], mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Row {row.name}: formula {row['transform_args']!r} does not parse: {e}")
        unknown = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - allowed
        if unknown:
            raise ValueError(
                f"Row {row.name}: formula {row['transform_args']!r} references unknown name(s) {sorted(unknown)} "
                f"-- not CSV columns or builtins"
            )


def _check_machine_keys(df: pd.DataFrame, data: pd.DataFrame) -> None:
    """Machine keys in errors/source dicts should name machines observed in the data (typo guard)."""
    machine_rows = df.loc[df["imas_path"] == "summary/machine", "csv_column"]
    if machine_rows.empty:
        return  # run_migration raises later if errors/dict-source are used without a machine row
    observed = set(data[machine_rows.iloc[0]].dropna())
    for _, row in df.iterrows():
        for label, d in (("errors", row["_errors"]), ("source", row["source"])):
            if isinstance(d, dict):
                unknown = set(d) - observed - {"default"}
                if unknown:
                    print(
                        f"WARNING: Row {row.name}: {label} dict for column '{row['csv_column']}' has machine "
                        f"key(s) {sorted(unknown)} not present in the data -- they will never match"
                    )


def _check_sidecar_names(df: pd.DataFrame, sidecar: dict[str, dict]) -> None:
    """Sidecar entries should name a crosswalk variable, else a rename has orphaned them."""
    known = df.attrs.get("all_csv_columns", set(df["csv_column"].astype(str)))
    for section, entries in sidecar.items():
        if section == "database":
            continue  # a whole-database description, not keyed by csv_column
        unknown = sorted(set(entries) - known)
        if unknown:
            print(
                f"WARNING: sidecar '{section}' has entr(ies) {unknown} matching no csv_column "
                f"in the crosswalk -- they will never be applied"
            )


def validate(df: pd.DataFrame, data: pd.DataFrame, factory: imas.IDSFactory, sidecar: dict[str, dict]) -> None:
    """Upfront validation of the crosswalk against the data CSV, the Data Dictionary and the sidecar."""
    # Upfront validation: all csv_columns must exist in data.
    missing_cols = set(df["csv_column"]) - set(data.columns)
    if missing_cols:
        raise ValueError(f"csv_column(s) not found in data CSV: {missing_cols}")

    _check_dd_paths(df, factory)
    _check_dictionary_coverage(df, data)
    _check_formula_identifiers(df, data)
    _check_machine_keys(df, data)
    _check_sidecar_names(df, sidecar)

    # Upfront validation: manifest rows must have a csv_dtype string.
    is_temp = df["status"] == "manifest"
    bad_dtype = is_temp & ~df["csv_dtype"].apply(lambda x: isinstance(x, str) and x.strip() != "")
    if bad_dtype.any():
        print(f"WARNING: status 'manifest' but csv_dtype is missing for rows: {list(df.index[bad_dtype])} -- skipping")


def temp_var_name(cw_row: pd.Series) -> tuple[str, str]:
    """Resolve a manifest row's identifier/name and its provenance kind.

    Returns (standard name, "standard_name") when the sidecar's `standard_names` section has an
    entry for this row's csv_column, else (csv_column, "db_variable").
    """
    std_name = cw_row.get("_standard_name")
    if isinstance(std_name, str) and std_name.strip():
        return std_name.strip(), "standard_name"
    return cw_row["csv_column"], "db_variable"


def build_write_spec(df: pd.DataFrame) -> pd.DataFrame:
    """Build the per-row write spec columns used by the main loop:

      _paths      : imas_path(s) to write to ("&"-split for multi-target rows)
      _value_leaf : leaf appended to each path for the value ("" = bare node)
      _str_descs  : pre-classified string descriptors  (leaf_segments, str)
      _num_descs  : pre-classified numeric descriptors (leaf_segments, number)
      _dict_descs : pre-classified dict descriptors    (leaf_segments, dict)

    Manifest rows are folded into the same structure with a stable AoS index, assigned here
    in crosswalk order so a variable keeps the same slot in every pulse (deterministic layout).
    """
    temp_idx: dict[str, int] = {}  # dictionary to track temp paths: indices
    temp_seen: dict[str, Any] = {}  # resolved "temporary/..." path -> first row (clash warning)
    specs: list[tuple[list[str], str, list[Descriptor]]] = []
    for _, cw_row in df.iterrows():
        if cw_row["status"] == "manifest":
            bucket_raw = cw_row["csv_dtype"]
            if not isinstance(bucket_raw, str) or bucket_raw.strip() == "":
                specs.append(([], "value", []))  # missing csv_dtype, already warned upfront
                continue
            bucket_base, _ = parse_seg(bucket_raw)
            if "(:)" in bucket_raw:
                idx = temp_idx.get(bucket_base, 0)
                temp_idx[bucket_base] = idx + 1
                resolved = f"{bucket_base}({idx})"
            else:
                resolved = bucket_raw  # explicit (N) or bare slot 0
            path = f"temporary/{resolved}"
            if path in temp_seen:
                print(
                    f"WARNING: temporary path '{path}' used by rows {temp_seen[path]} "
                    f"and {cw_row.name} -- values will overwrite; use (:) in csv_dtype "
                    f"for append behaviour"
                )
            else:
                temp_seen[path] = cw_row.name
            name_val, _ = temp_var_name(cw_row)
            descriptors = [(["identifier", "name"], name_val)]
            if isinstance(cw_row["csv_description"], str):
                descriptors.append((["identifier", "description"], cw_row["csv_description"]))
            # Dynamic temporary buckets (dynamic_float1d/dynamic_integer1d) hold their per-slice
            # series under value/data (over a local value/time); constant buckets store a scalar value.
            value_leaf = "value/data" if bucket_base.startswith("dynamic_") else "value"
            specs.append(([path], value_leaf, descriptors))
        else:
            imas_path = cw_row["imas_path"]
            if not isinstance(imas_path, str):
                raise ValueError(f"Row {cw_row.name}: imas_path is missing")
            paths = imas_path.split("&") if "&" in imas_path else [imas_path]
            if cw_row["_needs_source"]:
                source = cw_row["source"]
                has_source = isinstance(source, (str, dict)) or (is_number(source) and pd.notna(source))
                descriptors = [(cw_row["_source_pair"][1].split("/"), source)] if has_source else []
                specs.append((paths, cw_row["_source_pair"][0], descriptors))
            else:
                specs.append((paths, "", []))

    all_paths, all_leaves, all_descs = zip(*specs) if specs else ((), (), ())
    idx = df.index
    df["_paths"] = pd.Series(all_paths, index=idx, dtype=object)
    df["_value_leaf"] = pd.Series(all_leaves, index=idx, dtype=object)
    df["_str_descs"] = pd.Series(
        [[d for d in ds if isinstance(d[1], str)] for ds in all_descs], index=idx, dtype=object
    )
    df["_num_descs"] = pd.Series([[d for d in ds if is_number(d[1])] for ds in all_descs], index=idx, dtype=object)
    df["_dict_descs"] = pd.Series(
        [[d for d in ds if isinstance(d[1], dict)] for ds in all_descs], index=idx, dtype=object
    )
    return df


def process_pulse(
    data_row: pd.Series,
    crosswalk: pd.DataFrame,
    factory: imas.IDSFactory,
    machine_col: str | None = None,
    *,
    pulse_ids: dict[str, IDS] | None = None,
    context: WriteContext = WriteContext(),
    database_comment: str | None = None,
) -> dict[str, IDS]:
    """Write one data row (= one time-slice) into the per-root IDS dict for a pulse.

    All values and their companion descriptors (provenance strings, identifier names) are
    written into the pulse IDS at `context.slice_index`. Pass a shared `pulse_ids` and increasing
    `context.slice_index` to accumulate a pulse's time-slices into one IDS set; the default
    (fresh dict, single slice) reproduces the original one-IDS-per-row behaviour.

    `database_comment` (see `format_database_comment`) is stamped onto every newly created root's
    `ids_properties.comment`.
    """
    if pulse_ids is None:
        pulse_ids = {}
    machine = data_row.get(machine_col) if machine_col else None
    for _, cw_row in crosswalk.iterrows():
        value = data_row[cw_row["csv_column"]]
        value_present = not pd.isna(value)

        # A source value equal to one of the row's sentinels is a deliberate no-data placeholder:
        # treat it as missing so the leaf falls back to the IMAS empty.
        sentinels = cw_row.get("_sentinels")
        if value_present and sentinels and value in sentinels:
            value_present = False

        # Numeric companions are constant values, seeded into the pulse IDS even when the row
        # itself has no value for this pulse; string/dict companions are gated on the value.
        numeric_desc = cw_row["_num_descs"]
        if not (value_present or numeric_desc):
            continue

        row_context = context._replace(label=cw_row["csv_column"])
        value_leaf = cw_row["_value_leaf"]
        depth = len(value_leaf.split("/")) if value_leaf else 0

        for imas_path in cw_row["_paths"]:
            ids_root = imas_path.split("/")[0]
            if ids_root not in pulse_ids:
                pulse_ids[ids_root] = new_ids(factory, ids_root)
            ids = pulse_ids[ids_root]
            value_branch = (imas_path + ("/" + value_leaf if value_leaf else "")).split("/")[1:]

            if numeric_desc:
                placeholder = replace_wildcard_index(value_branch, 0)
                write_descriptors(ids, [(placeholder, None)], numeric_desc, depth, row_context)

            if not value_present:
                continue

            # Expand the row's transform into concrete (branch, value) writes.
            writes = resolve_writes(value_branch, value, cw_row, data_row)
            write_values(ids, writes, row_context)

            # Write per-machine error bars to the "_error_upper" extension of each leaf path.
            # errors holds {machine: spec}; error_bar() resolves each spec against the leaf
            # value (relative, range, or absolute -- see check_errors / docs/migration.md).
            errors = cw_row["_errors"]
            if errors is not None and writes and machine in errors:  # machine miss -> skip silently
                error_writes = [
                    (branch[:-1] + [branch[-1] + "_error_upper"], error_bar(errors[machine], v))
                    for branch, v in writes
                    if isinstance(v, (int, float, np.number, np.ndarray)) and not isinstance(v, bool)
                ]
                if error_writes:
                    write_values(ids, error_writes, row_context)

            # String companions, plus machine-specific (dict) ones resolved for this pulse.
            descriptors = cw_row["_str_descs"]
            if cw_row["_dict_descs"]:
                descriptors = descriptors + [
                    (leaf, d[machine] if machine in d else d["default"])
                    for leaf, d in cw_row["_dict_descs"]
                    if machine in d or "default" in d
                ]
            if descriptors and writes:
                write_descriptors(ids, writes, descriptors, depth, row_context)

    return pulse_ids


# ---------------------------------------------------------------------------
# SimDB ingestion (optional --simdb step)
# ---------------------------------------------------------------------------


def extract_variables(temporary_ids: IDS) -> dict:
    """Read scalars out of an in-memory `temporary` IDS into a {name: value} dict.

    Mirrors the original two-stage pipeline (simdb_ingest._read_temporary_scalars) but reads
    the IDS already in memory rather than from disk, so names/values stay identical.
    """
    result: dict[str, Any] = {}
    for el in temporary_ids.constant_float0d:
        name = str(el.identifier.name)
        if name and el.value.has_value:
            result[name] = float(el.value)
    for el in temporary_ids.constant_integer0d:
        name = str(el.identifier.name)
        if name and el.value.has_value:
            result[name] = int(el.value)
    for el in temporary_ids.constant_string0d:
        name = str(el.identifier.name)
        if name and el.value.has_value:
            result[name] = str(el.value)
    # constant_string1d holds a per-time-slice string series (no dynamic_string1d array exists).
    for el in temporary_ids.constant_string1d:
        name = str(el.identifier.name)
        if name and el.value.has_value:
            result[name] = np.asarray(el.value)
    # Dynamic temporary quantities carry a per-time-slice series; expose them as np.ndarray
    # (the manifest metadata stores native types and ndarrays, but treats lists as nested structure).
    for el in temporary_ids.dynamic_float1d:
        name = str(el.identifier.name)
        if name and el.value.data.has_value:
            result[name] = np.asarray(el.value.data, dtype=float)
    for el in temporary_ids.dynamic_integer1d:
        name = str(el.identifier.name)
        if name and el.value.data.has_value:
            result[name] = np.asarray(el.value.data, dtype=int)
    return result


def make_manifest(
    pulse_dir: pathlib.Path,
    dataset: str,
    machine: str,
    alias: str,
    variables: dict,
    name_kind: dict[str, str],
) -> "Manifest":
    """Build a SimDB Manifest for one migrated pulse (one entry per pulse).

    `variables` (from extract_variables) is split into "standard_name" and "db_variable"
    metadata groups per `name_kind` (from temp_var_name), so each is queryable as
    standard_name.<name> or db_variable.<name>.
    """
    metadata = [
        {"dataset": dataset},
        {"machine": machine},
        {"code": {"name": "idsmigration", "version": ""}},
        {"description": f"{machine} pulse from the {dataset} database migrated to IMAS HDF5."},
    ]
    standard_vars = {name: v for name, v in variables.items() if name_kind.get(name) == "standard_name"}
    dbvariable_vars = {
        name: v for name, v in variables.items() if name_kind.get(name, "db_variable") != "standard_name"
    }
    if standard_vars:
        metadata.append({"standard_name": standard_vars})
    if dbvariable_vars:
        metadata.append({"db_variable": dbvariable_vars})
    uri = f"imas:hdf5?path={pathlib.Path(pulse_dir).resolve().as_posix()}#summary"
    data = {
        "manifest_version": 2,
        "alias": alias,
        "inputs": [],
        "outputs": [{"uri": uri}],
        "metadata": metadata,
    }
    m = Manifest()
    m._data = data
    m._path = pathlib.Path(pulse_dir).resolve() / "manifest.yaml"
    m._metadata = {"metadata": metadata}
    return m


def set_temporary_local_time(temp_ids: IDS, times: Any) -> None:
    """Give each populated dynamic temporary signal the pulse's time vector (its local value/time)."""
    t = np.asarray(times, dtype=float)
    for bucket in ("dynamic_float1d", "dynamic_integer1d"):
        for el in getattr(temp_ids, bucket):
            if el.value.data.has_value:
                el.value.time = t


def write_pulse_dir(output_dir: pathlib.Path, name: str, pulse_ids: dict[str, IDS]) -> pathlib.Path:
    """Write a pulse's IDS set to its own HDF5 directory (pulse=0) and return the directory."""
    pulse_dir = output_dir / name
    pulse_dir.mkdir(parents=True, exist_ok=True)
    with imas.DBEntry(f"imas:hdf5?path={pulse_dir};pulse=0", "w") as entry:
        for ids in pulse_ids.values():
            entry.put(ids)
    return pulse_dir


def simdb_ingest(db: Any, config: Any, manifest: "Manifest", alias: str) -> bool:
    """Insert one pulse into SimDB, overwriting any existing entry for `alias`. Returns success."""
    try:
        try:
            db.delete_simulation(alias)  # overwrite: drop any existing entry for this alias
        except DatabaseError:
            pass  # no existing entry to replace
        db.insert_simulation(Simulation(manifest, config))
        return True
    except Exception as exc:
        _print_over_progress(f"  SimDB ingest FAILED for {alias}: {exc}")
        return False


def run_migration(
    crosswalk: pd.DataFrame,
    data: pd.DataFrame,
    factory: imas.IDSFactory,
    output_dir: pathlib.Path,
    dataset: str = "",
    simdb_enabled: bool = False,
    config: Any = None,
    db: Any = None,
    per_time_slice: bool = True,
    resolve_spec: dict[str, dict] | None = None,
    database_comment: str | None = None,
) -> None:
    """Build and write each pulse to disk immediately, without accumulating in memory.

    Default: one IDS set per pulse (rows grouped by (machine, pulse), dynamic nodes carry the
    ordered time-slices). With `--per-time-slice`, one IDS set per CSV row.

    When `simdb_enabled`, the in-memory `temporary` IDS is diverted into the SimDB manifest as
    `variables` metadata instead of being written to disk, and one SimDB entry is ingested per
    pulse. The `summary` IDS is always written to disk (SimDB catalogues it by reference).

    `database_comment` (see `format_database_comment`) is stamped onto every root's
    `ids_properties.comment` in every pulse.
    """
    # name -> "standard_name"/"db_variable", used to split SimDB manifest variables (see make_manifest).
    temp_name_kind = dict(
        temp_var_name(cw_row) for _, cw_row in crosswalk.loc[crosswalk["status"] == "manifest"].iterrows()
    )

    def first_csv_column(mask: Any) -> Any:
        hits = crosswalk.loc[mask, "csv_column"]
        return hits.iloc[0] if len(hits) else None

    def _maps_summary_time(p: Any) -> bool:
        return isinstance(p, str) and any(seg.strip().startswith("summary/time") for seg in p.split("&"))

    machine_col = first_csv_column(crosswalk["imas_path"] == "summary/machine")
    if machine_col is None and crosswalk["_errors"].notna().any():
        raise ValueError("errors column is used but no row maps to 'summary/machine' to key the lookup")
    if machine_col is None and crosswalk["_dict_descs"].apply(bool).any():
        raise ValueError("dict source is used but no row maps to 'summary/machine' to key the lookup")
    if simdb_enabled and machine_col is None:
        raise ValueError("--simdb is set but no row maps to 'summary/machine' to label each entry")

    # Pulse-grouping columns: SHOT->summary/pulse keys the group; TIME->summary/time orders slices.
    pulse_col = first_csv_column(crosswalk["imas_path"] == "summary/pulse")
    time_col = first_csv_column(crosswalk["imas_path"].apply(_maps_summary_time))
    if not per_time_slice and pulse_col is None:
        raise ValueError("no row maps to 'summary/pulse' to group time-slices")
    if not per_time_slice and machine_col is None:
        raise ValueError("no row maps to 'summary/machine' to group/name pulses")
    if not per_time_slice and time_col is None:
        print("WARNING: no row maps to 'summary/time' -- slices kept in CSV order")

    # Both modes yield (dir_name, machine, alias, progress_label, pulse_ids) for the shared writer below.
    def pulse_units(groups: list) -> Any:
        for (machine, pulse), gdf in groups:
            if isinstance(pulse, float) and pulse.is_integer():
                pulse = int(pulse)  # groupby key is float when the pulse column has any NaN elsewhere
            rows = [row for _, row in gdf.iterrows() if not row.isna().all()]
            if not rows:
                continue
            if time_col is not None:  # insert slices in ascending time order (CSV order is not trusted)
                rows.sort(key=lambda row: (pd.isna(row[time_col]), row[time_col]))
            n = len(rows)
            times = [row[time_col] for row in rows] if time_col is not None else list(range(n))
            pulse_key = f"{machine}/{pulse}"

            pulse_ids: dict[str, IDS] = {}
            for ti, row in enumerate(rows):
                context = WriteContext(slice_index=ti, n_slices=n, pulse=pulse_key, resolve_spec=resolve_spec)
                process_pulse(
                    row,
                    crosswalk,
                    factory,
                    machine_col,
                    pulse_ids=pulse_ids,
                    context=context,
                    database_comment=database_comment,
                )
            for ids in pulse_ids.values():
                backfill_time(ids, times)
            if "temporary" in pulse_ids:
                set_temporary_local_time(pulse_ids["temporary"], times)

            dir_name = f"{str(machine).lower()}_{pulse}"
            yield dir_name, str(machine), f"{dataset}/{machine}/{pulse}", pulse_key, pulse_ids

    def row_units() -> Any:
        counters: dict[str, int] = {}  # per-machine entry index for the SimDB alias
        for pulse_idx, data_row in data.iterrows():
            if data_row.isna().all():
                continue
            pulse_ids = process_pulse(data_row, crosswalk, factory, machine_col, database_comment=database_comment)
            for ids in pulse_ids.values():
                backfill_time(ids)

            machine = str(data_row[machine_col]) if machine_col else ""
            index = counters.get(machine, 0)
            counters[machine] = index + 1
            dir_name = f"pulse_{pulse_idx:04d}"
            yield dir_name, machine, f"{dataset}-{machine.lower()}-{index}", pulse_idx, pulse_ids

    if per_time_slice:  # one IDS set per CSV row
        total, units = len(data), row_units()
    else:
        groups = list(data.groupby([machine_col, pulse_col], sort=False))
        total, units = len(groups), pulse_units(groups)

    output_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    ingested = 0
    failed: list[str] = []
    start = time.monotonic()
    last_report = start

    for dir_name, machine, alias, progress_label, pulse_ids in units:
        temp_ids = pulse_ids.pop("temporary", None) if simdb_enabled else None
        pulse_dir = write_pulse_dir(output_dir, dir_name, pulse_ids)
        done += 1

        if simdb_enabled:
            variables = extract_variables(temp_ids) if temp_ids is not None else {}
            manifest = make_manifest(pulse_dir, dataset, machine, alias, variables, temp_name_kind)
            if simdb_ingest(db, config, manifest, alias):
                ingested += 1
            else:
                failed.append(dir_name)

        last_report = report_progress(done, total, progress_label, start, last_report)

    report_summary("Processed", done, total, start, f" to {output_dir}")
    if simdb_enabled:
        report_summary("Ingested", ingested, done, start, " into SimDB")
        if failed:
            print("Failed to ingest:", failed)
    report_conflict_summary()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the CSV -> IDS migration pipeline."""
    global VERBOSE
    args = parse_args()
    VERBOSE = args.verbose
    mapping_path = ROOT / "resources" / "mappings" / args.mapping
    output_dir = ROOT / "resources" / "results" / args.experiment
    sidecar = load_sidecar(mapping_path)
    crosswalk = load_crosswalk(mapping_path, sidecar)
    data = load_dataset(ROOT / "resources" / "input" / args.dataset)
    factory = imas.IDSFactory(version=args.dd_version)
    resolve_value_leaves(crosswalk, factory)
    validate(crosswalk, data, factory, sidecar)
    crosswalk = build_write_spec(crosswalk)

    if args.simdb and not SIMDB_AVAILABLE:
        print("WARNING: --simdb given but the 'simdb' package is not importable -- skipping ingestion")
    simdb_enabled = args.simdb and SIMDB_AVAILABLE
    config = db = None
    if simdb_enabled:
        config = Config()
        db = get_local_db(config)

    run_migration(
        crosswalk,
        data,
        factory,
        output_dir,
        dataset=args.experiment,
        simdb_enabled=simdb_enabled,
        config=config,
        db=db,
        per_time_slice=args.per_time_slice,
        resolve_spec=sidecar["resolve"],
        database_comment=format_database_comment(sidecar["database"]),
    )


if __name__ == "__main__":
    main()
