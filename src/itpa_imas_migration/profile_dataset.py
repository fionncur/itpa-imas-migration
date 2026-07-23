#!/usr/bin/env python3
"""profile_dataset: per-column statistics dump for a scalar database CSV.

Reads the original data .csv and writes a markdown profile per dataset:
automatic NA-marker detection, multi-signal sentinel detection, full categorical value
tables, and per-machine statistics. 

Usage:
    python idstools/scripts/temporary/profile_dataset [-d tc26_data.csv]
                                                      [--machine-col TOK]
                                                      [-o resources/profiles]
"""

import argparse
import math
import pathlib
import re

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]

MACHINE_COL_CANDIDATES = ["TOK", "MACHINE", "DEVICE"]

# Sentinel lexical forms: all-9s mantissa, exact +/-10^k, IMAS empties.
ALL9_RE = re.compile(r"^9+(\.9*)?$")
IMAS_EMPTY_FLOAT = -9.0e40
IMAS_EMPTY_INT = -999999999


def fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def is_na_marker_form(s: str) -> bool:
    """Non-linguistic form: no letters or digits at all, or a single repeated character."""
    stripped = s.strip()
    if not stripped:
        return True
    if not any(c.isalnum() for c in stripped):
        return True
    return len(set(stripped)) == 1


def lexical_sentinel(v: float) -> str | None:
    if v == IMAS_EMPTY_FLOAT or (abs(v) >= 8.9e40 and abs(v) <= 9.1e40):
        return "IMAS empty float"
    if v == IMAS_EMPTY_INT:
        return "IMAS empty int"
    av = abs(v)
    if av > 0:
        exp = math.log10(av)
        if abs(exp - round(exp)) < 1e-9:
            return f"exact {'-' if v < 0 else ''}10^{round(exp)}"
    mantissa = f"{av:.6g}".replace("e", "").replace("+", "").replace("-", "")
    if ALL9_RE.match(f"{av:g}") or ALL9_RE.match(mantissa[:6]):
        return "all-9s"
    return None


def profile_numeric(col: str, values: pd.Series, machines: pd.Series, lines: list[str]) -> list[str]:
    """Numeric column body; returns index flags."""
    flags = []
    n = len(values)
    n_distinct = values.nunique()

    continuous = n_distinct > max(20, 0.05 * n)
    sentinels = {}  # value -> (verdict, [signal descriptions])
    if continuous and n >= 10:
        counts = values.value_counts()
        # Low-precision data (2-3 significant digits) repeats exact values quite often, so the
        # candidate threshold scales with the column's typical multiplicity n/n_distinct:
        # a candidate must repeat ~5x more often than an ordinary value of this column does.
        threshold = max(3, math.ceil(5 * n / n_distinct))
        candidates = counts[(counts >= threshold) & (counts >= 0.005 * n)]
        for cand, cnt in candidates.items():
            rest = values[values != cand]
            if len(rest) < 5:
                continue
            med, q25, q75 = rest.median(), rest.quantile(0.25), rest.quantile(0.75)
            iqr = q75 - q25
            signals = []
            if iqr > 0 and (cand < med - 10 * iqr or cand > med + 10 * iqr):
                signals.append(f"extremity ({fmt(cand)} vs median {fmt(med)} +/- 10*IQR)")
            elif cand == values.min() or cand == values.max():
                signals.append(f"global {'min' if cand == values.min() else 'max'}")
            if cand != 0 and ((rest > 0).all() and cand < 0 or (rest < 0).all() and cand > 0):
                signals.append("sign opposite to all remaining values")
            if cand != 0 and med != 0 and abs(math.log10(abs(cand)) - math.log10(abs(med))) >= 6:
                signals.append(f"magnitude {fmt(abs(math.log10(abs(cand)) - math.log10(abs(med))))} decades off median")
            lex = lexical_sentinel(float(cand))
            if lex:
                signals.append(f"lexical form: {lex}")
            cand_machines = machines[values.index[values == cand]].value_counts()
            all_machines = machines[values.index].value_counts()
            if 0 < len(cand_machines) < len(all_machines):
                signals.append("machine-exclusive: " + ", ".join(f"{m} x{c}" for m, c in cand_machines.items()))
            non_lex = [s for s in signals if not s.startswith("lexical") and not s.startswith("machine-exclusive")]
            if lex and len(signals) >= 2:
                verdict = "sentinel"
            elif len(non_lex) >= 2:
                verdict = "sentinel"
            elif len(non_lex) == 1:
                verdict = "suspected"
            else:
                verdict = "repeated"
            sentinels[cand] = (verdict, cnt, signals)

    flagged = {v for v, (verdict, _, _) in sentinels.items() if verdict in ("sentinel", "suspected")}

    def stat_line(vals: pd.Series, label: str) -> str:
        return (
            f"- {label}: min={fmt(vals.min())}  q25={fmt(vals.quantile(0.25))}  median={fmt(vals.median())}  "
            f"q75={fmt(vals.quantile(0.75))}  max={fmt(vals.max())}  mean={fmt(vals.mean())}  std={fmt(vals.std())}"
        )

    lines.append(stat_line(values, "stats (raw)"))
    if flagged:
        clean = values[~values.isin(flagged)]
        if len(clean):
            lines.append(stat_line(clean, f"stats (excluding {', '.join(fmt(v) for v in sorted(flagged))})"))
        flags.append("sentinels")

    for cand, (verdict, cnt, signals) in sorted(sentinels.items(), key=lambda kv: -kv[1][1]):
        if verdict == "repeated":
            continue
        lines.append(f"- **{verdict}** {fmt(cand)} x{cnt}: " + "; ".join(signals))
    repeated = [(v, c) for v, (verdict, c, _) in sentinels.items() if verdict == "repeated"]
    if repeated:
        lines.append(
            "- repeated values (no sentinel signals): "
            + ", ".join(f"{fmt(v)} x{c}" for v, c in sorted(repeated, key=lambda t: -t[1])[:5])
        )

    per_machine = []
    for m, grp in values.groupby(machines[values.index]):
        per_machine.append(f"  - {m}: n={len(grp)}, median={fmt(grp.median())} [{fmt(grp.min())}, {fmt(grp.max())}]")
    if per_machine:
        lines.append("- per machine:")
        lines.extend(sorted(per_machine))
    return flags


def profile_string(col: str, values: pd.Series, lines: list[str]) -> list[str]:
    flags = []
    n_distinct = values.nunique()
    ws_variants = sum(1 for v in values.unique() if isinstance(v, str) and v != v.strip())
    if ws_variants:
        flags.append("whitespace")
        lines.append(f"- {ws_variants} distinct value(s) carry leading/trailing whitespace")
    if n_distinct > 200:
        lines.append(f"- identifier-like ({n_distinct} distinct values); examples:")
        for v, c in values.value_counts().head(10).items():
            lines.append(f"  - {v!r} x{c}")
    else:
        lines.append(f"- all {n_distinct} unique values:")
        for v, c in values.value_counts().items():
            lines.append(f"  - {v!r} x{c}")
    return flags


def profile(data: pd.DataFrame, machine_col: str, dataset_name: str) -> str:
    machines = data[machine_col].astype(str).str.strip()
    header = [
        f"# Profile: {dataset_name}",
        "",
        f"- rows: {len(data)}, columns: {len(data.columns)}",
        f"- machine column: `{machine_col}`",
        "- machines: " + ", ".join(f"{m} ({c})" for m, c in machines.value_counts().items()),
        "",
    ]

    na_markers_global: dict[str, int] = {}
    index_rows = []
    body: list[str] = []

    for col in data.columns:
        raw = data[col]
        blank = raw.isna() | (raw.astype(str).str.strip() == "")
        present = raw[~blank]
        numeric = pd.to_numeric(present, errors="coerce")
        unparsed = present[numeric.isna()]
        # NA markers are split off before type classification, so a marker-heavy float
        # column (e.g. ZEFF: 199 x '-') is not mistaken for a string column.
        markers, anomalies = {}, {}
        for v, c in unparsed.astype(str).value_counts().items():
            (markers if is_na_marker_form(v) else anomalies)[v] = c
        n_real = len(present) - sum(markers.values())
        parse_rate = numeric.notna().sum() / n_real if n_real else 0.0

        lines = [f"## {col}", ""]
        flags: list[str] = []

        if parse_rate >= 0.90 and n_real:
            for v, c in markers.items():
                na_markers_global[v] = na_markers_global.get(v, 0) + c
            if markers:
                lines.append("- NA markers detected: " + ", ".join(f"{v!r} x{c}" for v, c in markers.items()))
                flags.append("na-markers")
            if anomalies:
                lines.append(
                    "- **anomalous non-numeric values**: " + ", ".join(f"{v!r} x{c}" for v, c in anomalies.items())
                )
                flags.append("anomalies")
            values = numeric.dropna()
            int_form = present[numeric.notna()].astype(str).str.strip().str.fullmatch(r"[+-]?\d+").all()
            base_dtype = "int" if int_form else "float"
            dtype = base_dtype if not anomalies else f"{base_dtype} ({sum(anomalies.values())} anomalous strings)"
            n_fill = len(values)
            lines.insert(
                1,
                f"- dtype: {dtype}, fill: {n_fill}/{len(data)} ({100 * n_fill / len(data):.0f}%), distinct: {values.nunique()}",
            )
            flags += profile_numeric(col, values, machines, lines)
            median = values.median() if len(values) else float("nan")
        else:
            values = present.astype(str)
            base_dtype = "string"
            n_fill = len(values)
            lines.insert(
                1,
                f"- dtype: string, fill: {n_fill}/{len(data)} ({100 * n_fill / len(data):.0f}%), distinct: {values.nunique()}",
            )
            flags += profile_string(col, values, lines)
            median = float("nan")

        if n_fill and values.nunique() == 1:
            flags.append("constant")
        index_rows.append(
            (
                col,
                base_dtype,
                f"{100 * n_fill / len(data):.0f}%",
                values.nunique() if n_fill else 0,
                fmt(median),
                " ".join(sorted(set(flags))) or "-",
            )
        )
        body.extend(lines + [""])

    index = [
        "## Index",
        "",
        "| column | dtype | fill | distinct | median | flags |",
        "|---|---|---|---|---|---|",
    ]
    index += [f"| {c} | {d} | {f} | {u} | {m} | {fl} |" for c, d, f, u, m, fl in index_rows]
    index.append("")
    if na_markers_global:
        index.append(
            "**Detected NA markers (dataset-wide):** "
            + ", ".join(f"{v!r} x{c}" for v, c in sorted(na_markers_global.items(), key=lambda t: -t[1]))
        )
        index.append("")

    return "\n".join(header + index + body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-column statistics profile of a dataset CSV.")
    parser.add_argument(
        "-d", "--dataset", default="tc26_data.csv", help="CSV under resources/input/ (default: %(default)s)"
    )
    parser.add_argument("--machine-col", default=None, help="Machine column name (default: auto-detect)")
    parser.add_argument("-o", "--output-dir", default=str(ROOT / "resources" / "profiles"))
    args = parser.parse_args()

    data_path = ROOT / "resources" / "input" / args.dataset
    data = pd.read_csv(data_path, dtype=object, keep_default_na=False, na_values=[""])
    machine_col = args.machine_col or next((c for c in MACHINE_COL_CANDIDATES if c in data.columns), None)
    if machine_col is None:
        raise SystemExit(f"No machine column found (tried {MACHINE_COL_CANDIDATES}); pass --machine-col")

    out_dir = pathlib.Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (pathlib.Path(args.dataset).stem + ".md")
    out_path.write_text(profile(data, machine_col, args.dataset), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
