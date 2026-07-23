# IDS Migration: Crosswalk Format and Script Reference

The `idsmigration` script converts tabular experimental data (CSV) into IMAS IDS objects written to HDF5.  It is driven entirely by a spreadsheet called the **crosswalk** (e.g. `resources/mappings/tc26_crosswalk.xlsx`). Everything below describes how to read existing crosswalks and create new ones.

## Crosswalk columns

| Column               | Type                 | Purpose |
| -------------------- | -------------------- | ------- |
| `csv_column`         | str                  | Column/variable name in the source CSV |
| `csv_unit`           | str                  | Unit of the source value |
| `imas_unit`          | str                  | Unit of the IDS target field (automatically populated by DD) |
| `csv_dtype`          | str                  | Storage bucket for `status=temporary` rows (see [Temporary IDSs](#temporary-idss)) |
| `imas_dtype`         | str                  | IDS data type (automatically populated by DD) |
| `imas_path`          | str                  | Target path inside the IDS (see [Path notation](#path-notation-and-indexing)) |
| `csv_description`    | str                  | Description of the source variable from the original database. |
| `imas_description`   | str                  | Description of the IDS target field (automatically populated by DD) |
| `imas_standard_name` | str                  | IMAS standard name for the variable; used as `identifier/name` for `temporary` rows (falls back to `csv_column` when blank) |
| `kind`               | str                  | `constant`, `dynamic`, or `static` (automatically populated by DD) |
| `status`             | str                  | `mapped`, `mapped_caveat`, `temporary`, `derived` or `discard` (see [Status values](#status-values)) |
| `notes`              | str                  | Free-text notes, caveats, warnings, etc. |
| `transform`          | str                  | `identity`, `dictionary`, or `formula` (see [Transform types](#transform-types)) |
| `transform_args`     | str                  | Arguments for the transform (dict literal or Python expression). |
| `needs_source`       | bool                 | When `True`, write a value plus a companion sibling instead of a bare value (see [Value/source pairs](#valuesource-pairs)) |
| `source_fields`      | str                  | Optional `("value_leaf", "source_leaf")` 2-tuple naming the sibling leaves written when `needs_source=True`; defaults to `("value", "source")` |
| `source`             | str, number, or dict | Value written to the companion sibling leaf when `needs_source=True`. A **string** or **number** is written into every pulse IDS. A **dict** is a `{machine: descriptor}` literal looked up per-pulse via the value mapped to `summary/machine` (see [Value/source pairs](#sibling-pair-writes-needs_source-and-source_fields)) |
| `errors`             | str                  | Optional `{machine: error}` dict literal giving per-machine error bars (see [Error bars](#error-bars-errors)) |

## Transform types

### `identity`

Copies the CSV value directly to the IDS path.  No `transform_args` needed.

```
csv_column: IP
imas_path:  summary/global_quantities/ip
transform:  identity
```

### `dictionary`

Maps discrete CSV values to IDS values via a Python dict in `transform_args`. The string is parsed with `ast.literal_eval()` and must be a valid Python dict literal; any other result will raise a `ValueError` at parse time.

```
csv_column:     WALMAT
imas_path:      summary/wall/material/index
transform:      dictionary
transform_args: {"MO": 11, "W": 2, "Be": 10}
```

String cells are stripped of leading/trailing whitespace at load (the CSVs are padded), so dictionary keys are authored against the stripped values (`"MO"`, never `" MO"`). If a CSV value is not present as a key, the row is skipped for that pulse rather than raising an error. All uncovered values are reported upfront, with counts, by the pre-run validation (see [Upfront validation](#upfront-validation)); the runtime skip itself is silent.

#### Dictionary of lists

When a dict value is itself a Python list, the script performs a **many-to-one expansion**: it iterates the list and writes each element to a separate array-of-structures slot, using wildcard index replacement on the `imas_path` (see [Wildcard indexing](#wildcard-indexing-)).

```
csv_column:     AUXHEAT
imas_path:      core_sources/source(:)/identifier/index
transform:      dictionary
transform_args: {"NB": 2, "IC": 5, "EC": 3,
                 "NBIC": [2, 5], "NBEC": [2, 3],
                 "ECIC": [3, 5], "NBICEC": [2, 5, 3]}
```

`AUXHEAT = "NBIC"` resolves to `[2, 5]` and writes:

- `core_sources/source(0)/identifier/index = 2`
- `core_sources/source(1)/identifier/index = 5`

Single-value entries (e.g. `"NB": 2`) are treated equivalently to a one-element list and always land at index `0`.

### `formula`

Evaluates an arbitrary Python expression over the source row.  Every CSV column is bound as a **bare variable** named after the csv_column, so the expression can combine several columns:

```
csv_column:     TIME_X
transform:      formula
transform_args: TIME_X - TIME_Y
```

This writes `data_row["TIME_X"] - data_row["TIME_Y"]` to the target path. Python builtins such as `abs`, `min`, `max`, and `round` are available (e.g. `abs(TIME_X - TIME_Y)`).

`csv_column` is still required and acts as the **primary** column: if its value is missing (NaN) for a given pulse, the data cell is skipped for that pulse (same as every other transform).  Set it to one of the columns the formula uses.

Notes and limitations:

- Only columns whose names are **valid Python identifiers** can be referenced (no spaces, no leading digits).
- The primary `csv_column` is validated at startup; other columns named in the expression are resolved at run time.  A typo or a bad expression raises a `ValueError` naming the offending row and formula.

---

## Path notation and indexing

`imas_path` uses `/` for hierarchy and parenthetical suffixes `()` for array indexing:

### Fixed indexing `(n)`

```
nbi/unit(0)/energy/data
```

Always targets element `n` of the AoS.  The array is resized if needed.

### Wildcard indexing `(:)`

```
core_sources/source(:)/identifier/index
```

The `:` is a placeholder resolved at write time.  For `imas_path` wildcards the index comes from the position in the list produced by the dictionary-of-lists expansion (`enumerate(value)`), so the first element lands at `(0)`, the second at `(1)`, and so on.  Two crosswalk rows that both carry `(:)` in their paths are independent — each starts from `0`.

Wildcards are resolved segment-by-segment by `replace_wildcard_index()`: it replaces only the `(:)` suffix of the matched segment (e.g.`source(:)` → `source(0)`), leaving the rest of the path untouched.

### Multiple target paths `&`

A single crosswalk row can fan out to several IDS paths by separating them with `&`.  The same (transformed) value is written to every path.

```
imas_path: summary/time(0)&equilibrium/time(0)&divertors/time(0)
```

Each path may belong to a different top-level IDS; the script creates IDS objects on demand.

---

## Sibling-pair writes (`needs_source` and `source_fields`)

Many IDS nodes pair a measured value with a companion string (provenance, label, etc.) as two sibling leaves under a shared parent. When `needs_source = True`, the script writes to **two** leaves at the same level instead of one bare scalar:

1. The **value leaf** receives the transformed CSV value.
2. The **companion leaf** receives the string from the `source` column.

By default the two leaves are `value` and `source`:

```
# needs_source=True, source_fields blank, imas_path = "summary/global_quantities/ip"
summary/global_quantities/ip/value  ← transformed CSV value
summary/global_quantities/ip/source ← row["source"], e.g. "experiment"
```

### Customising the leaf names with `source_fields`

Set `source_fields` to a 2-tuple of strings to name the leaves explicitly.  The **first** element is the value leaf; the **second** is the companion leaf.  The cell is parsed with `ast.literal_eval()` and must be a 2-tuple of strings; anything else raises a `ValueError` at startup naming the offending `csv_column`.

```
# needs_source=True, source_fields = ("name", "description")
# imas_path = "divertors/divertor(0)/identifier"
divertors/divertor(0)/identifier/name        ← transformed CSV value
divertors/divertor(0)/identifier/description ← row["source"]
```

This applies to every transform type (`identity`, `dictionary` including dictionary-of-lists, and `formula`).  For dictionary-of-lists, the companion string is written alongside each expanded AoS slot.

If `source` is blank, the companion leaf is omitted and the value is written as a bare node (same as `needs_source = False`).

The companion (source) string is written into **every pulse** alongside the value leaf, when the value is present.

### Numeric `source`: a constant companion value

When `source` is a **number** rather than a string, it is treated as a real value rather than a provenance label.  It is written to the companion leaf in **every pulse** alongside the value leaf.  This is the eval-free way to attach a fixed constant to a sibling node.

```
# needs_source=True, source_fields = ("rho_tor", "rho_tor_norm"), source = 0.95
# csv_column = R95, imas_path = "summary/local/pedestal/position"
summary/local/pedestal/position/rho_tor      ← transformed CSV value (R95, per pulse)
summary/local/pedestal/position/rho_tor_norm ← 0.95 (constant, in every pulse)
```

A numeric companion is **ungated**: it is written into every pulse even when the primary `csv_column` value is missing for that pulse (the value leaf is simply left empty).  This makes it a way to stamp a fixed constant into each IDS.  (String companions, by contrast, are only written when the row has a real value.)

So the companion gating is asymmetric by type:

- **string** `source` → written into the pulse when the value is present;
- **numeric** `source` → written into every pulse, unconditionally;
- **dict** `source` → resolved per pulse by machine name, written to the pulse IDS when the value is present (see below).

**Constraint:** both named leaves must exist as sub-fields of the `imas_path` node.  Check the Data Dictionary before setting `needs_source` on a new row.

### Machine-specific source

When provenance differs across machines, set `source` to a Python dict literal mapping machine name to the provenance string.  The cell is parsed with `ast.literal_eval()` and must be a valid dict literal.

An optional `"default"` key applies to any machine that has no more specific entry of its own.  A machine-specific entry always overrides `"default"` entirely (it is not joined or concatenated) — use it for the machines whose provenance genuinely differs from the rest.

```
# needs_source=True, source = {"default": "EFIT", "JET": "JFIT"}
# imas_path = "summary/global_quantities/ip"
summary/global_quantities/ip/value  ← transformed CSV value (per pulse)
summary/global_quantities/ip/source ← "JFIT" for JET pulses, "EFIT" for every other machine
```

A dict source is resolved per pulse against the pulse's machine name and written into the pulse IDS: the machine's own key wins if present, otherwise `"default"` is used, otherwise the companion leaf is skipped for that pulse.  A `summary/machine` mapping row is required; the script raises at load if none exists and a dict source is in use.

---

## Error bars (`errors`)

Many IDS leaves carry an uncertainty in a sibling field: for a value at `IMAS_PATH`, the upper error bar lives at `IMAS_PATH + "_error_upper"`. Because the confinement database spans multiple devices with different measurement uncertainties, error bars are authored **per machine**.

Set `errors` to a dict literal mapping machine name to an error **spec**. It is parsed with `ast.literal_eval()`. Each spec is one of three forms:

| Spec     | Literal           | Bar written |
| -------- | ----------------- | ----------- |
| Relative | `0.03` (= 3 %)    | `abs(value) * 0.03` |
| Range    | `[0.10, 0.20]`    | `abs(value) * max(range)` — conservative upper bound; the min is discarded |
| Absolute | `{"abs": 300000}` | the value verbatim, in IDS units (independent of `value`) |

```
# imas_path = "summary/global_quantities/ip"
# errors = {"JET": 0.05, "AUG": 0.03, "TFTR": {"abs": 300000}, "D3D": [0.10, 0.20]}
```

For each pulse the script looks up that pulse's machine (the value mapped to `summary/machine`) and, if it is a key, writes the resolved bar to the `_error_upper` sibling of wherever the row's value landed:

- bare node → `<node>_error_upper`;
- `needs_source` value/source pair → `value_error_upper` (matching the DD layout).

Behaviour:

- **Blank cell** → nothing extra written (normal behaviour).
- **Machine not in the dict** → no error written for that pulse, silently.

Relative and range bars are *relative*, so the absolute bar tracks the actual datum, including the post-transform result for `dictionary`/`formula` rows. An **absolute** spec is written verbatim and must already be in IDS units — the author pre-converts (e.g. cm→m, kW→W); `csv_unit`/`imas_unit` are documentation-only columns in the crosswalk, not an automatic conversion the script applies. The lookup requires a row mapping to `summary/machine`; the script raises at load if `errors` is used without one.

Errors that cannot be expressed in these three forms — compound (`±15% abs + ±2% rel`), value-conditional (phase-dependent), formula-dependent (`±0.05/bp`), or unquantified — are left as free-text notes rather than encoded.

---

## Temporary IDSs

Rows with `status = temporary` are **not** written to a named IDS in the physics hierarchy.  Instead they are stored in IMAS's `temporary` IDS, which provides generic typed buckets for values of arbitrary dimensionality (0-D scalars through 5-D arrays).

The `csv_dtype` column names the bucket and its indexing mode:

| `csv_dtype`                     | Meaning |
| ------------------------------- | ------- |
| `constant_float0d(:)`           | Float scalar slot at a stable index (assigned in crosswalk order) |
| `constant_string0d(:)`          | String scalar slot at a stable index (assigned in crosswalk order) |
| `constant_string1d(:)`          | String 1-D array slot; in per-pulse mode, each element corresponds to one time-slice (no `dynamic_string1d` array exists in the DD) |
| `dynamic_float1d(:)`            | Float time-series slot; stores per-slice values in `value/data` with a shared `value/time` axis |
| `dynamic_integer1d(:)`          | Integer time-series slot; same layout as `dynamic_float1d` |
| `constant_float0d(2)`           | Fix to slot 2; array is resized to at least 3 with `keep=True`, leaving intermediate slots empty if not yet filled |
| `constant_float0d` *(no index)* | Always write to slot 0; warns if two rows clash on the same bare bucket |

The `constant_*` buckets store a single scalar per pulse.  The `dynamic_*` buckets accumulate one value per time-slice and are only meaningful in the default per-pulse mode (they reduce to a one-element array in `--per-time-slice` mode).  When the script is run with `--simdb`, all populated bucket types (constant and dynamic) are extracted into the manifest's `variables.*` metadata.

The `(:)` suffix assigns a **stable index**, keyed by the segment name before `(:)` (e.g. `constant_float0d`).  Indices are assigned once, in crosswalk (row) order — **not** per pulse — so a given variable occupies the **same** slot in every pulse.  A pulse that lacks data for a variable simply leaves that slot empty.  This gives a deterministic, consistent layout across all pulses.

As with physics-IDS rows, both the value and the descriptor strings (identifier name/description) are written directly into the pulse:

```
constant_float0d(n)/value                  ← transformed CSV value
constant_float0d(n)/identifier/name        ← imas_standard_name (if set), else csv_column
constant_float0d(n)/identifier/description ← csv_description (if present)

dynamic_float1d(n)/value/data              ← transformed CSV value (one element per time-slice)
dynamic_float1d(n)/value/time              ← pulse's time vector (set after all slices are processed)
dynamic_float1d(n)/identifier/name         ← imas_standard_name (if set), else csv_column
```

The `imas_path` column is ignored for temporary rows; the entire path is derived from `csv_dtype`.  Temporary rows are otherwise processed identically to physics-IDS rows (same transforms, same value/descriptor split).

### Diversion under `--simdb`

By default the `temporary` IDS is written to the HDF5 backend like any other root.  When the script is run with `--simdb` (see [SimDB ingestion](#simdb-ingestion---simdb)), the `temporary` IDS is built in memory exactly as above but **not** written to disk; instead its scalars are read back out (`identifier/name` → `value`) and attached to the pulse's SimDB manifest as `standard_name.*`/`dbvariable.*` metadata (see [SimDB ingestion](#simdb-ingestion---simdb) for how that split is decided).  `csv_dtype` still drives the in-memory layout in both cases.

---

## Status values

| Status          | Behaviour |
| --------------- | --------- |
| `mapped`        | Primary, authoritative mapping to the IDS hierarchy. |
| `mapped_caveat` | Written to the IDS but subject to known caveats (sign conventions, approximations). See `notes`. |
| `temporary`     | Stored in the `temporary` IDS instead of a physics IDS.  Useful for quantities that have no stable IMAS path yet. |
| `derived`       | Not currently implemented; row is skipped.  Reserved for quantities that must be computed from other fields. |

Rows without a recognised `transform` value (`identity`, `dictionary`, `formula`) are also silently excluded from processing.

---

## Many-to-one transformations in the crosswalk

The crosswalk is **one-row-per-source-column**, not one-row-per-target-path.  A single source column can write to multiple targets in two complementary ways:

1. **`&`-separated paths** in `imas_path` — same value, multiple destinations.
2. **Dictionary of lists** — one source value expands into multiple elements of an AoS via wildcard indexing.

Both mechanisms are resolved within `resolve_writes()` and require no special columns beyond those already described.

---

## Upfront validation

Before any pulse is written, `validate()` checks the crosswalk against the data CSV and the Data
Dictionary (at `--dd-version`). Fatal problems **raise** (the migration would crash or write garbage
anyway); recoverable ones **warn** and continue:

| Check | Behaviour |
| ----- | --------- |
| Every `csv_column` exists in the data CSV | raise |
| Every `imas_path` (after `&` split and index stripping) exists in the DD; for `needs_source` rows both `source_fields` leaves exist under the node | raise |
| Formula `transform_args` parse, and every bare name is a CSV column or Python builtin | raise |
| Dictionary / formula rows have a `transform_args` string | raise |
| Dictionary keys cover every value observed in the data column | warn, listing each uncovered value with its count (those rows are skipped silently at run time) |
| Machine keys in `errors` and dict-valued `source` cells name machines observed in the data (`"default"` exempt) | warn (a key that never matches writes nothing) |
| `needs_source` rows have a usable `source` | warn, companion leaf not written |
| `temporary` rows have a `csv_dtype` | warn, row skipped |

---

## Running the script

The script is a command-line tool.

```bash
# defaults: per-pulse grouping (one IDS set per machine/pulse combination)
python idstools/scripts/bin/idsmigration

# override inputs / behaviour
python idstools/scripts/bin/idsmigration -e 2008 -d 2008_data.csv -m 2008_crosswalk.xlsx \
    --dd-version 4.1.1

# one-IDS-per-row (old behaviour, restored with --per-time-slice)
python idstools/scripts/bin/idsmigration --per-time-slice
```

Run `python idstools/scripts/bin/idsmigration -h` for the full help. The arguments and their defaults are:

| Argument             | Default               | Purpose |
| -------------------- | --------------------- | ------- |
| `-e`, `--experiment` | `2008`                | Sub-folder under `resources/results/` for output |
| `-d`, `--dataset`    | `2008_data.csv`       | Input CSV filename under `resources/input/` |
| `-m`, `--mapping`    | `2008_crosswalk.xlsx` | Crosswalk spreadsheet filename under `resources/mappings/` |
| `--dd-version`       | `4.1.1`               | Data Dictionary version used to build the IDS factory |
| `--simdb`            | off                   | Ingest each migrated pulse into the local SimDB (see below) |
| `--per-time-slice`   | off                   | Write one IDS set per CSV row instead of one per `(machine, pulse)` group |

### Default mode: per-pulse grouping

By default the script groups CSV rows by `(machine, pulse)`, sorts each group in ascending time order, and writes **one IDS set per pulse**.  Dynamic IDS nodes (those whose `kind` is `dynamic`) accumulate one value per time-slice; static and constant nodes are written once and checked for consistency across slices (a warning is printed if a "constant" quantity differs between slices, and the first-seen value is kept).

The crosswalk must contain rows mapping to `summary/machine` and `summary/pulse` for grouping to work.  A `summary/time` mapping is optional but recommended — without it, slices are kept in CSV order and the `summary/time` vector is absent.

Output is one directory per `(machine, pulse)` pair, named `{machine}_{pulse}`:

```
resources/results/tc26/
  aug_12345/
  aug_12346/
  jet_99001/
  ...
```

### `--per-time-slice` mode (one IDS per row)

Restores the original behaviour: each CSV row becomes an independent IDS set, written to a sequentially-numbered directory:

```
resources/results/2008/
  pulse_0000/
  pulse_0001/
  ...
```

Each directory is a valid IMAS DBEntry accessible via:

```python
uri = "imas:hdf5?path=resources/results/2008/pulse_0000;pulse=0"
with imas.DBEntry(uri, "r") as entry:
    summary = entry.get("summary")
```

---

## SimDB ingestion (`--simdb`)

`simdb` is an **optional** dependency.  When it is importable and `--simdb` is passed, the migration ingests **one SimDB entry per pulse** as it runs.  If `--simdb` is given but the package is not importable, the script prints a warning and continues the migration with ingestion disabled.

Each entry's manifest carries:

| Field                                   | Source |
| --------------------------------------- | ------ |
| `alias`                                 | **default mode:** `{dataset}/{machine}/{pulse}` — **`--per-time-slice` mode:** `{dataset}-{machine}-{index}`, where `dataset` is the `--experiment` value and `index` is a per-machine counter |
| `metadata.dataset` / `metadata.machine` | the experiment label and the pulse's `summary/machine` value |
| `metadata.standard_name.*`              | temporary quantities diverted from the in-memory `temporary` IDS (see [Diversion under `--simdb`](#diversion-under---simdb)) whose crosswalk row has an `imas_standard_name`, keyed by that standard name |
| `metadata.dbvariable.*`                 | the same, for temporary quantities with no `imas_standard_name`, keyed by `csv_column` instead |
| `outputs.uri`                           | `imas:hdf5?path=<pulse_dir>#summary` — a **reference** to the on-disk summary IDS |

Each temporary quantity lands in exactly one of the two groups, decided per-row by `temp_var_name()`: a set `imas_standard_name` sends it to `standard_name.<name>`; a blank one falls back to `dbvariable.<csv_column>`.  This keeps quantities with an agreed IMAS standard name distinguishable, when queried later, from ad-hoc database columns that don't have one yet — e.g. `simdb simulation query standard_name.loss_power=...` vs `dbvariable.SELEC2007=...`.

SimDB is a metadata catalogue: it stores the manifest plus a checksummed *reference* to the `summary` IDS, not its array data.  The `summary` IDS is therefore always written to HDF5, with or without `--simdb`; only the `temporary` IDS write is suppressed when ingesting.  A `summary/machine` mapping row is required; the script raises at load if `--simdb` is used without one.