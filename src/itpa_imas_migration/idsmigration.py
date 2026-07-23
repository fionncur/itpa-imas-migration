#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""idsmigration -- convert tabular experimental data (CSV) into IMAS IDS objects.

Driven by a crosswalk spreadsheet that maps source CSV columns to IDS paths and
transforms. The pipeline (see `main`): load the crosswalk and dataset, validate,
build a per-row write spec, build one set of IDSs per pulse in memory, then write
the pulses to HDF5.

See docs/migration.md for the crosswalk format and full behaviour reference.
"""

from typing import Any
import argparse
import ast
import re
import logging
import time
import pathlib
from datetime import datetime, timedelta
import imas
import pandas as pd
import numpy as np

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
mappings_dir = ROOT / "resources" / "mappings"
input_dir = ROOT / "resources" / "input"

# Type aliases for the structures passed between the helpers below.
IDS = Any  # an IMAS top-level IDS object (summary, equilibrium, ...)
Branch = list[str]  # ordered node segments, e.g. ["divertor(0)", "value"]
Write = tuple[Branch, Any]  # a (branch, value) pair destined for one leaf
Descriptor = tuple[Branch, Any]  # a (leaf_segments, value) pair written alongside the value in the pulse IDS


def is_number(x: Any) -> bool:
    """True for a real int/float, excluding bool (an int subclass)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


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
            f"source_fields {source_fields_val!r} for csv_column '{csv_column}' is not a valid Python literal: {e}"
        )
    if not isinstance(parsed, tuple) or len(parsed) != 2 or not all(isinstance(p, str) for p in parsed):
        raise ValueError(
            f"source_fields for csv_column '{csv_column}' must be a 2-tuple of "
            f"strings, e.g. ('name', 'description'); got {parsed!r}"
        )
    return parsed


def _validate_error_spec(machine: str, spec: Any) -> None:
    """Validate one {machine: spec} value. Accepts a relative float, a 2-element numeric
    range [min, max], or an absolute {"abs": value}; anything else raises ValueError."""
    if is_number(spec):
        return
    if isinstance(spec, (list, tuple)) and len(spec) == 2 and all(is_number(p) for p in spec):
        return
    if isinstance(spec, dict) and set(spec) == {"abs"} and is_number(spec["abs"]):
        return
    raise ValueError(
        f"errors spec for machine '{machine}' must be a relative float, a 2-element "
        f"numeric range [min, max], or an absolute {{'abs': value}}; got {spec!r}"
    )


def parse_errors(errors_val: Any) -> dict | None:
    """Parse an `errors` cell into a {machine: spec} dict. Blank/NaN -> None.

    Each spec is a relative float (error = |value| * rel), a 2-element range [min, max]
    (relative; the conservative max is used), or an absolute {"abs": value} written
    verbatim in IDS units. See docs/migration.md.
    """
    if not isinstance(errors_val, str) or errors_val.strip() == "":
        return None
    try:
        parsed = ast.literal_eval(errors_val.strip())
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"errors {errors_val!r} is not a valid Python literal: {e}")
    if not isinstance(parsed, dict):
        raise ValueError(f"errors must be a dict literal, e.g. {{'JET': 0.05}}; got {parsed!r}")
    for machine, spec in parsed.items():
        _validate_error_spec(machine, spec)
    return parsed


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


def new_ids(factory: imas.IDSFactory, name: str) -> IDS:
    """Create a fresh top-level IDS with homogeneous time mode set."""
    ids = factory.new(name)
    ids.ids_properties.homogeneous_time = imas.ids_defs.IDS_TIME_MODE_HOMOGENEOUS
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
    return node, branch[-1]


def _np_dtype(data_type: Any) -> Any:
    """numpy dtype for an IDS numeric leaf (int leaves -> int32, everything else -> float)."""
    return np.int32 if data_type.name == "INT" else np.float64


def _empty_fill(data_type: Any) -> Any:
    """IMAS empty placeholder for an IDS numeric leaf, used to pad missing time-slices."""
    return imas.ids_defs.EMPTY_INT if data_type.name == "INT" else imas.ids_defs.EMPTY_FLOAT


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


def set_slice(parent: IDS, leaf: str, target: Any, value: Any, slice_index: int, n_slices: int) -> None:
    """Place `value` at `slice_index`, growing the leaf to `n_slices` with appropriate padding.

    Numeric leaves use IMAS empty placeholders; string leaves pad with "".
    """
    if target.metadata.data_type.name == "STR":
        cur = list(target.value) if target.has_value else []
        if len(cur) < n_slices:
            cur += [""] * (n_slices - len(cur))
    else:
        data_type = target.metadata.data_type
        cur = (
            np.atleast_1d(np.array(target.value, copy=True))
            if target.has_value
            else np.empty(0, dtype=_np_dtype(data_type))
        )
        if cur.size < n_slices:
            grown = np.full(n_slices, _empty_fill(data_type), dtype=_np_dtype(data_type))
            if cur.size:
                grown[: cur.size] = cur
            cur = grown
    cur[slice_index] = value
    setattr(parent, leaf, cur)


def set_leaf(
    ids: IDS,
    branch: Branch,
    value: Any,
    slice_index: int = 0,
    n_slices: int = 1,
    const_ctx: str | None = None,
    label: str | None = None,
) -> None:
    """Write `value` to the leaf at `branch` for time-slice `slice_index` of an `n_slices` pulse.

    Dynamic numeric leaves are filled by array position; constant/static leaves are written once
    and, when `n_slices > 1`, checked for agreement across slices (warn and keep first on mismatch).
    """
    parent, leaf_seg = resolve_parent(ids, branch)
    leaf, _ = parse_seg(leaf_seg)
    target = getattr(parent, leaf)

    if isinstance(target, imas.ids_struct_array.IDSStructArray):
        if target.metadata.type.is_dynamic:
            if len(target) <= slice_index:
                target.resize(slice_index + 1, keep=True)
            target[slice_index] = value
        else:
            if len(target) == 0:
                target.resize(1)
            target[0] = value
    elif isinstance(target, imas.ids_primitive.IDSNumericArray):
        if target.metadata.type.is_dynamic:
            set_slice(parent, leaf, target, value, slice_index, n_slices)
        else:
            setattr(parent, leaf, np.atleast_1d(value))
    elif target.metadata.data_type.name == "STR" and target.metadata.ndim >= 1:
        set_slice(parent, leaf, target, value, slice_index, n_slices)
    else:
        # Scalar / string leaf: constant or static. Write once; warn if it disagrees across slices.
        if n_slices > 1 and target.has_value:
            # Identify the quantity by its crosswalk variable name
            name = label or "/".join(branch)
            if not _values_equal(target.value, value) and (const_ctx, name) not in _warned_const_mismatch:
                _warned_const_mismatch.add((const_ctx, name))
                print(
                    f"WARNING: constant '{name}' differs across slices of pulse "
                    f"{const_ctx}: {target.value!r} vs {value!r} -- keeping first"
                )
            return  # keep the first value
        setattr(parent, leaf, value)


def write_values(
    ids: IDS,
    writes: list[Write],
    slice_index: int = 0,
    n_slices: int = 1,
    const_ctx: str | None = None,
    label: str | None = None,
) -> None:
    """Write resolved (branch, value) pairs to their leaves in the pulse `ids`."""
    for branch, value in writes:
        set_leaf(ids, branch, value, slice_index, n_slices, const_ctx, label)


def write_companions(
    ids: IDS,
    anchor: Branch,
    descriptors: list[Descriptor],
    slice_index: int = 0,
    n_slices: int = 1,
    const_ctx: str | None = None,
    label: str | None = None,
) -> None:
    """Write each companion (leaf_segments, value) at `anchor`, the shared parent node."""
    for desc_leaf, desc_val in descriptors:
        set_leaf(ids, anchor + list(desc_leaf), desc_val, slice_index, n_slices, const_ctx, label)


def write_descriptors(
    ids: IDS,
    writes: list[Write],
    descriptors: list[Descriptor],
    value_leaf_depth: int,
    slice_index: int = 0,
    n_slices: int = 1,
    const_ctx: str | None = None,
    label: str | None = None,
) -> None:
    """Write each descriptor (leaf_segments, value) into the pulse IDS alongside values.

    Each is written at the sibling of every value's parent node (branch[:-1]), so it
    lands at the correct path for each expanded AoS slot -- e.g. the source sibling, or
    a temporary row's identifier name/description.
    """
    for branch, _ in writes:
        anchor = branch[: len(branch) - value_leaf_depth]
        write_companions(ids, anchor, descriptors, slice_index, n_slices, const_ctx, label)


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


_warned_const_mismatch: set[tuple[Any, str]] = set()  # (pulse ctx, leaf path) already warned about


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
            raise ValueError(f"Row {cw_row.name}: formula {cw_row['transform_args']!r} failed: {exc}") from exc
        return [(ids_branch, result)]
    raise ValueError(f"Row {cw_row.name}: unhandled transform '{cw_row['transform']}'")


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


def report_progress(
    count: int, total: int, label: Any, start: float, last_report: float, interval: float = 5.0
) -> float:
    """Timed, counted progress for the long pulse loops (build-in-memory and write-to-disk)."""
    now = time.monotonic()
    if now - last_report < interval:
        return last_report
    elapsed = now - start
    rate = count / elapsed if elapsed > 0 else 0
    eta = (total - count) / rate if rate > 0 else 0
    pct = 100 * count / total
    print(
        f"Progress: {count:{len(str(total))}d}/{total} ({pct:3.1f}%)  pulse {label}  "
        f"elapsed {timedelta(seconds=int(elapsed))}  "
        f"ETA {timedelta(seconds=int(eta))}  "
        f"({rate:.1f} pulses/s)"
    )
    return now


def report_summary(verb: str, count: int, total: int, start: float, suffix: str = "") -> None:
    """Closing one-line summary for a pulse loop."""
    print(f"{verb} {count}/{total} pulses{suffix} in {timedelta(seconds=int(time.monotonic() - start))}")


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
        help="Ingest each migrated pulse into the local SimDB; diverts temporary quantities "
        "into manifest variables instead of a temporary IDS \t(requires the simdb package)",
    )
    parser.add_argument(
        "--per-time-slice",
        action="store_true",
        help="Group CSV rows by (machine, pulse) and write one IDS set per pulse with all its "
        "time-slices, instead of one IDS set per row",
    )
    return parser.parse_args()


def resolve_io_paths(args: argparse.Namespace) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Resolve (data_path, mapping_path, output_dir) from the parsed CLI args."""
    data_path = input_dir / args.dataset
    mapping_path = mappings_dir / args.mapping
    output_dir = ROOT / "resources" / "results" / args.experiment
    return data_path, mapping_path, output_dir


def load_crosswalk(mapping_path: str) -> pd.DataFrame:
    """Read the crosswalk xlsx, keep implemented+accepted rows, and parse source pairs."""
    df = pd.read_excel(mapping_path)

    # Convert needs_source to boolean.
    df["needs_source"] = df["needs_source"].fillna(False).astype(bool)

    # Parse source cells that are dict literals (machine-specific provenance).
    df["source"] = df["source"].map(_try_parse_dict)

    # Include only implemented transforms (identity/dictionary/formula) with an accepted status.
    keep_mask = df["transform"].isin(["identity", "dictionary", "formula"]) & df["status"].isin(
        ["mapped", "temporary", "mapped_caveat"]
    )
    df = df[keep_mask]

    # Parse the optional source_fields column into (value_leaf, source_leaf) pairs.
    # Blank/NaN or non-needs_source rows -> ("value", "source").
    if "source_fields" in df.columns:
        pairs = [
            parse_source_pair(sf, col) if ns else ("value", "source")
            for sf, col, ns in zip(df["source_fields"], df["csv_column"], df["needs_source"])
        ]
    else:
        pairs = [("value", "source")] * len(df)
    df["_source_pair"] = pd.Series(pairs, index=df.index, dtype=object)

    # Parse the optional errors column into {machine: spec} dicts (or None).
    if "errors" in df.columns:
        errors = [parse_errors(e) for e in df["errors"]]
    else:
        errors = [None] * len(df)
    df["_errors"] = pd.Series(errors, index=df.index, dtype=object)
    return df


def load_dataset(data_path: str) -> pd.DataFrame:
    """Import experimental data from csv and strip leading/trailing whitespace from string cells."""
    data = pd.read_csv(data_path, na_values=["-", "????????", "??????????", "********", "."])
    return data.map(lambda x: (x.strip() or np.nan) if isinstance(x, str) else x)


def _check_dd_paths(df: pd.DataFrame, factory: imas.IDSFactory) -> None:
    """Every imas_path must exist in the DD; needs_source rows need both leaves."""
    ids_cache: dict[str, Any] = {}
    bad: list[str] = []
    for _, row in df[df["status"] != "temporary"].iterrows():
        if not isinstance(row["imas_path"], str):
            continue
        for path in row["imas_path"].split("&"):
            segments = [parse_seg(seg)[0] for seg in path.strip().split("/")]
            root, node_path = segments[0], "/".join(segments[1:])
            try:
                if root not in ids_cache:
                    ids_cache[root] = factory.new(root)
                node_meta = ids_cache[root].metadata[node_path]
            except (KeyError, imas.exception.IDSNameError):
                bad.append(f"row {row.name} ('{row['csv_column']}'): '{path}' not in DD")
                continue
            if row["needs_source"]:
                for leaf in row["_source_pair"]:
                    try:
                        node_meta[leaf]
                    except KeyError:
                        bad.append(
                            f"row {row.name} ('{row['csv_column']}'): '{path}' has no '{leaf}' sub-field required by needs_source"
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
        uncovered = observed[~observed.isin(dictionary.keys())].value_counts()
        if len(uncovered):
            details = ", ".join(f"{v!r} x{c}" for v, c in uncovered.items())
            print(
                f"WARNING: Row {row.name}: dictionary for column '{row['csv_column']}' does not cover "
                f"observed value(s) {details} -- these rows will be skipped"
            )


def _check_formula_identifiers(df: pd.DataFrame, data: pd.DataFrame) -> None:
    """Every bare name in a formula must be a CSV column or a Python builtin."""
    import builtins

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


def validate(df: pd.DataFrame, data: pd.DataFrame, factory: imas.IDSFactory) -> pd.DataFrame:
    """Upfront validation; warns and fixes up the `source` column in place where needed."""
    # Upfront validation: all csv_columns must exist in data.
    missing_cols = set(df["csv_column"]) - set(data.columns)
    if missing_cols:
        raise ValueError(f"csv_column(s) not found in data CSV: {missing_cols}")

    _check_dd_paths(df, factory)
    _check_dictionary_coverage(df, data)
    _check_formula_identifiers(df, data)
    _check_machine_keys(df, data)

    # Upfront validation: needs_source rows must have a source value -- either a descriptor string
    # written into each pulse, a numeric value written per pulse, or a machine-keyed dict.
    def _has_source(x: Any) -> bool:
        return isinstance(x, (str, dict)) or (is_number(x) and pd.notna(x))

    bad_source = df["needs_source"] & ~df["source"].apply(_has_source)
    if bad_source.any():
        print(
            f"WARNING: needs_source=True but source is missing for rows: {list(df.index[bad_source])} -- companion leaf will not be written"
        )

    # Upfront validation: temporary rows must have a csv_dtype string.
    is_temp = df["status"] == "temporary"
    bad_dtype = is_temp & ~df["csv_dtype"].apply(lambda x: isinstance(x, str) and x.strip() != "")
    if bad_dtype.any():
        print(f"WARNING: status 'temporary' but csv_dtype is missing for rows: {list(df.index[bad_dtype])} -- skipping")

    return df


def temp_var_name(cw_row: pd.Series) -> tuple[str, str]:
    """Resolve a temporary row's identifier/name and its provenance kind.

    Returns (imas_standard_name, "standard_name") when set, else (csv_column, "db_variable").
    """
    std_name = cw_row.get("imas_standard_name", "")
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

    Temporary rows are folded into the same structure with a stable AoS index, assigned here
    in crosswalk order so a variable keeps the same slot in every pulse (deterministic layout).
    """
    temp_idx: dict[str, int] = {}  # dictionary to track temp paths: indices
    temp_seen: dict[str, Any] = {}  # resolved "temporary/..." path -> first row (clash warning)
    paths_list, value_leaf_list = [], []
    str_descs_list, num_descs_list, dict_descs_list = [], [], []
    for _, cw_row in df.iterrows():
        if cw_row["status"] == "temporary":
            bucket_raw = cw_row["csv_dtype"]
            if not isinstance(bucket_raw, str) or bucket_raw.strip() == "":
                paths_list.append([])  # missing csv_dtype, already warned upfront
                value_leaf_list.append("value")
                str_descs_list.append([])
                num_descs_list.append([])
                dict_descs_list.append([])
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
            paths_list.append([path])
            # Dynamic temporary buckets (dynamic_float1d/dynamic_integer1d) hold their per-slice
            # series under value/data (over a local value/time); constant buckets store a scalar value.
            value_leaf_list.append("value/data" if bucket_base.startswith("dynamic_") else "value")
            str_descs_list.append([d for d in descriptors if isinstance(d[1], str)])
            num_descs_list.append([d for d in descriptors if is_number(d[1])])
            dict_descs_list.append([d for d in descriptors if isinstance(d[1], dict)])
        else:
            imas_path = cw_row["imas_path"]
            if not isinstance(imas_path, str):
                raise ValueError(f"Row {cw_row.name}: imas_path is missing")
            paths_list.append(imas_path.split("&") if "&" in imas_path else [imas_path])
            if cw_row["needs_source"]:
                value_leaf_list.append(cw_row["_source_pair"][0])
                source = cw_row["source"]
                has_source = isinstance(source, (str, dict)) or (is_number(source) and pd.notna(source))
                descriptors = [(cw_row["_source_pair"][1].split("/"), source)] if has_source else []
            else:
                value_leaf_list.append("")
                descriptors = []
            str_descs_list.append([d for d in descriptors if isinstance(d[1], str)])
            num_descs_list.append([d for d in descriptors if is_number(d[1])])
            dict_descs_list.append([d for d in descriptors if isinstance(d[1], dict)])
    df["_paths"] = pd.Series(paths_list, index=df.index, dtype=object)
    df["_value_leaf"] = pd.Series(value_leaf_list, index=df.index, dtype=object)
    df["_str_descs"] = pd.Series(str_descs_list, index=df.index, dtype=object)
    df["_num_descs"] = pd.Series(num_descs_list, index=df.index, dtype=object)
    df["_dict_descs"] = pd.Series(dict_descs_list, index=df.index, dtype=object)
    return df


def process_pulse(
    data_row: pd.Series,
    crosswalk: pd.DataFrame,
    factory: imas.IDSFactory,
    machine_col: str | None = None,
    *,
    pulse_ids: dict[str, IDS] | None = None,
    slice_index: int = 0,
    n_slices: int = 1,
    const_ctx: str | None = None,
) -> dict[str, IDS]:
    """Write one data row (= one time-slice) into the per-root IDS dict for a pulse.

    All values and their companion descriptors (provenance strings, identifier names) are
    written into the pulse IDS at `slice_index`. Pass a shared `pulse_ids` and increasing
    `slice_index` to accumulate a pulse's time-slices into one IDS set; the default
    (fresh dict, single slice) reproduces the original one-IDS-per-row behaviour.
    """
    if pulse_ids is None:
        pulse_ids = {}
    for _, cw_row in crosswalk.iterrows():

        var_label = cw_row["csv_column"]
        value = data_row[cw_row["csv_column"]]
        value_present = not pd.isna(value)

        # Descriptor types pre-classified in build_write_spec; numeric ungated, string/dict gated on value.
        string_desc = cw_row["_str_descs"]
        numeric_desc = cw_row["_num_descs"]
        dict_desc = cw_row["_dict_descs"]

        for imas_path in cw_row["_paths"]:
            ids_root = imas_path.split("/")[0]
            value_leaf = cw_row["_value_leaf"]
            full_path = imas_path + ("/" + value_leaf if value_leaf else "")
            value_branch = full_path.split("/")[1:]
            depth = len(value_leaf.split("/")) if value_leaf else 0

            # Numeric companions are constant values, seeded into the pulse IDS even when
            # the row itself has no value for this pulse.
            if numeric_desc:
                if ids_root not in pulse_ids:
                    pulse_ids[ids_root] = new_ids(factory, ids_root)
                placeholder = replace_wildcard_index(value_branch, 0)
                write_descriptors(
                    pulse_ids[ids_root],
                    [(placeholder, None)],
                    numeric_desc,
                    depth,
                    slice_index,
                    n_slices,
                    const_ctx,
                    var_label,
                )

            if not value_present:
                continue

            # Expand the row's transform into concrete (branch, value) writes.
            writes = resolve_writes(value_branch, value, cw_row, data_row)

            # Write values to value path in the pulse IDS.
            if ids_root not in pulse_ids:
                pulse_ids[ids_root] = new_ids(factory, ids_root)
            write_values(pulse_ids[ids_root], writes, slice_index, n_slices, const_ctx, var_label)

            # Write per-machine error bars to the "_error_upper" extension of each leaf path.
            # errors holds {machine: spec}; error_bar() resolves each spec against the leaf
            # value (relative, range, or absolute -- see parse_errors / docs/migration.md).
            errors = cw_row["_errors"]
            if errors is not None and writes:
                machine = data_row.get(machine_col) if machine_col else None
                if machine in errors:  # miss -> skip silently
                    spec = errors[machine]
                    error_writes = [
                        (branch[:-1] + [branch[-1] + "_error_upper"], error_bar(spec, v))
                        for branch, v in writes
                        if isinstance(v, (int, float, np.number, np.ndarray)) and not isinstance(v, bool)
                    ]
                    if error_writes:
                        write_values(pulse_ids[ids_root], error_writes, slice_index, n_slices, const_ctx, var_label)

            # Write string and machine-specific (dict) companion descriptors to the pulse IDS.
            if string_desc and writes:
                write_descriptors(
                    pulse_ids[ids_root], writes, string_desc, depth, slice_index, n_slices, const_ctx, var_label
                )
            if dict_desc and writes:
                machine = data_row.get(machine_col) if machine_col else None
                resolved = [
                    (leaf, d[machine] if machine in d else d["default"])
                    for leaf, d in dict_desc
                    if machine in d or "default" in d
                ]
                if resolved:
                    write_descriptors(
                        pulse_ids[ids_root], writes, resolved, depth, slice_index, n_slices, const_ctx, var_label
                    )

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


def simdb_ingest(db: Any, config: Any, manifest: "Manifest", alias: str, label: str, failed: list[str]) -> bool:
    """Insert one pulse into SimDB, overwriting any existing entry for `alias`. Returns success."""
    try:
        try:
            db.delete_simulation(alias)  # overwrite: drop any existing entry for this alias
        except DatabaseError:
            pass  # no existing entry to replace
        db.insert_simulation(Simulation(manifest, config))
        return True
    except Exception as exc:
        print(f"  SimDB ingest FAILED for {alias}: {exc}")
        failed.append(label)
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
) -> None:
    """Build and write each pulse to disk immediately, without accumulating in memory.

    Default: one IDS set per pulse (rows grouped by (machine, pulse), dynamic nodes carry the
    ordered time-slices). With `--per-time-slice`, one IDS set per CSV row.

    When `simdb_enabled`, the in-memory `temporary` IDS is diverted into the SimDB manifest as
    `variables` metadata instead of being written to disk, and one SimDB entry is ingested per
    pulse. The `summary` IDS is always written to disk (SimDB catalogues it by reference).
    """
    # name -> "standard_name"/"db_variable", used to split SimDB manifest variables (see make_manifest).
    temp_name_kind = dict(
        temp_var_name(cw_row) for _, cw_row in crosswalk.loc[crosswalk["status"] == "temporary"].iterrows()
    )

    machine_rows = crosswalk.loc[crosswalk["imas_path"] == "summary/machine", "csv_column"]
    machine_col = machine_rows.iloc[0] if len(machine_rows) else None
    if machine_col is None and crosswalk["_errors"].notna().any():
        raise ValueError("errors column is used but no row maps to 'summary/machine' to key the lookup")
    if machine_col is None and crosswalk["_dict_descs"].apply(bool).any():
        raise ValueError("dict source is used but no row maps to 'summary/machine' to key the lookup")
    if simdb_enabled and machine_col is None:
        raise ValueError("--simdb is set but no row maps to 'summary/machine' to label each entry")

    # Pulse-grouping columns: SHOT->summary/pulse keys the group; TIME->summary/time orders slices.
    pulse_rows = crosswalk.loc[crosswalk["imas_path"] == "summary/pulse", "csv_column"]
    pulse_col = pulse_rows.iloc[0] if len(pulse_rows) else None

    def _maps_summary_time(p: Any) -> bool:
        return isinstance(p, str) and any(seg.strip().startswith("summary/time") for seg in p.split("&"))

    time_rows = crosswalk.loc[crosswalk["imas_path"].apply(_maps_summary_time), "csv_column"]
    time_col = time_rows.iloc[0] if len(time_rows) else None
    if not per_time_slice and pulse_col is None:
        raise ValueError("no row maps to 'summary/pulse' to group time-slices")
    if not per_time_slice and machine_col is None:
        raise ValueError("no row maps to 'summary/machine' to group/name pulses")
    if not per_time_slice and time_col is None:
        print("WARNING: no row maps to 'summary/time' -- slices kept in CSV order")

    output_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    counters: dict[str, int] = {}  # per-machine entry index for the SimDB alias (per-time-slice mode)
    ingested = 0
    failed: list[str] = []
    start = time.monotonic()
    last_report = start

    if not per_time_slice:
        groups = list(data.groupby([machine_col, pulse_col], sort=False))
        total = len(groups)
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
            ctx = f"{machine}/{pulse}"

            pulse_ids: dict[str, IDS] = {}
            for ti, row in enumerate(rows):
                process_pulse(
                    row,
                    crosswalk,
                    factory,
                    machine_col,
                    pulse_ids=pulse_ids,
                    slice_index=ti,
                    n_slices=n,
                    const_ctx=ctx,
                )
            for ids in pulse_ids.values():
                backfill_time(ids, times)
            if "temporary" in pulse_ids:
                set_temporary_local_time(pulse_ids["temporary"], times)

            temp_ids = pulse_ids.pop("temporary", None) if simdb_enabled else None
            pulse_dir = write_pulse_dir(output_dir, f"{str(machine).lower()}_{pulse}", pulse_ids)
            done += 1

            if simdb_enabled:
                alias = f"{dataset}/{machine}/{pulse}"
                variables = extract_variables(temp_ids) if temp_ids is not None else {}
                manifest = make_manifest(pulse_dir, dataset, str(machine), alias, variables, temp_name_kind)
                if simdb_ingest(db, config, manifest, alias, pulse_dir.name, failed):
                    ingested += 1

            last_report = report_progress(done, total, ctx, start, last_report)
    else:  # --per-time-slice: one IDS per CSV row
        total = len(data)
        for pulse_idx, data_row in data.iterrows():
            if data_row.isna().all():
                continue
            pulse_ids = process_pulse(data_row, crosswalk, factory, machine_col)
            for ids in pulse_ids.values():
                backfill_time(ids)

            temp_ids = pulse_ids.pop("temporary", None) if simdb_enabled else None
            pulse_dir = write_pulse_dir(output_dir, f"pulse_{pulse_idx:04d}", pulse_ids)
            done += 1

            if simdb_enabled:
                machine = str(data_row[machine_col])
                index = counters.get(machine, 0)
                counters[machine] = index + 1
                alias = f"{dataset}-{machine.lower()}-{index}"
                variables = extract_variables(temp_ids) if temp_ids is not None else {}
                manifest = make_manifest(pulse_dir, dataset, machine, alias, variables, temp_name_kind)
                if simdb_ingest(db, config, manifest, alias, f"pulse_{pulse_idx:04d}", failed):
                    ingested += 1

            last_report = report_progress(done, total, pulse_idx, start, last_report)

    report_summary("Processed", done, total, start, f" to {output_dir}")
    if simdb_enabled:
        report_summary("Ingested", ingested, done, start, " into SimDB")
        if failed:
            print("Failed to ingest:", failed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the CSV -> IDS migration pipeline."""
    args = parse_args()
    data_path, mapping_path, output_dir = resolve_io_paths(args)
    crosswalk = load_crosswalk(mapping_path)
    data = load_dataset(data_path)
    factory = imas.IDSFactory(version=args.dd_version)
    crosswalk = validate(crosswalk, data, factory)  # validate *does* modify the df in case of "bad_source"
    crosswalk = build_write_spec(crosswalk)

    simdb_enabled = args.simdb
    config = db = None
    if args.simdb and not SIMDB_AVAILABLE:
        print("WARNING: --simdb given but the 'simdb' package is not importable -- skipping ingestion")
        simdb_enabled = False
    elif simdb_enabled:
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
    )


if __name__ == "__main__":
    main()
