# Migration Data Logbook

**timeslice** = one CSV row.
**shot** = one `(TOK, SHOT)` pulse, spanning one or more timeslices.
**cell** = one column value in one row.

## 1. Per-file structural issues

### 1a. `2008_data.csv`

| Issue | Column | Description | Extent |
|---|---|---|---|
| Century typo | `DATE` | For all AUG, `209YMMDD` (in the future), impossible. $\rightarrow$ `19YMMDD` (i.e. `2092`–`2098` $\rightarrow$ `1992`–`1998`) | 557 timeslices / 156 shots |
| Mixed date format | `DATE` | Documented already in variable sheet, handled by crosswalk transformation. 6-digit `YYMMDD` (norm, 5863; ~6340 incl. 5-/7-digit leading-zero-stripped) vs 8-digit (687) | column-wide |
| Shot-number reuse | `SHOT` | TUMAN-3M SHOT=12 = two distinct discharges sharing SHOT+TIME=0.0494 | 2 timeslices / 1 shot id |
| Synthetic placeholders | `SHOT` | `DEMO-A/B/C/D`, `ITER`: blank/`1` SHOT, 2 density scenarios each | 10 timeslices / 5 pseudo-shots |
| Malformed text | `PHASE_RED` (crosswalk discarded) | literal `" is missing"`, `"HSElMA is missing"` | 4 timeslices |

### 1b. `tc26_data.csv`

| Issue | Column | Detail | Extent |
|---|---|---|---|
| Exact duplicate row | all | JET 98969 @ TIME=50.135s, byte-identical across all 30 cols | 2 timeslices / 1 shot |
| Whitespace in key | `TOK` | `" CMOD"` (leading space) vs `"CMOD"` | 25/77 CMOD timeslices |
| Undocumented column | `FRACNMIN` | Not listed in the TC26 variable definition sheet; meaning unknown, so not added to `TC26_crosswalk.xlsx`. Numeric, fill 656/689 (95%), range [0, 1.697] excluding a `-1e-08` sentinel (x52, CMOD-exclusive) -- name suggests a minority-species fraction, but unconfirmed. | all 689 rows |

## 2. Cross-dataset overlap

| Pair | Shared `(TOK,SHOT)` shots | Exact `(TOK,SHOT,TIME)` timeslices |
|---|---|---|
| 2008 ∩ h-mode | 244 | 344 |
| 2008 ∩ tc26 | 0 | 0 |
| tc26 ∩ h-mode | 9 | 0 |

## 3. Shared columns across the 344 (2008 ∩ h-mode) overlap timeslices

### 3a. Same name, different variable $\rightarrow$ do not merge by name

| Column | 2008 | h-mode |
|---|---|---|
| `SELDB1` | 10-digit bitmask string (`1111110111`) | 3-digit bitmask `{0,1,11,100,101,111}` |
| `DIVNAME` | `DIV-I`, `DIV-II`, `MkIIap`, `OPEN-DIV` | `DV-IPRE`, `DV-II-C`, `MARKIIAP`, `NONAME` |

`SELDB2` is a 10-digit bitmask in *both* DBs. But of course the meaning of the selection criteria (i.e. the meaning of each digit) differs between the DBs.

### 3b. `SEPLIM`: definitions match; discrepancy is ASDEX-only

| TOK | overlap ratio (hmode/2008 values) | verdict |
|---|---|---|
| AUG | ~1.00 | agree |
| JET | ~1.00 | agree |
| JFT2M | ~1.11 | ~11% off, likely genuine physical difference |
| ASDEX | 11.9–127.6, median 16.8, non-constant | not a unit factor; anomolous, further investigation needed |

### 3c. `PHASE`: labels agree on the H / non-H boundary

| Metric | Value |
|---|---|
| Raw string match | 217/344 |
| Coarsened (OHM/L/LH/H) match | 287/344 |
| Disagreements across H / non-H boundary | 0 |
| Remaining 57 mismatches | the L→H transition instant: 2008=`LH` vs h-mode=`L` |

### 3d. Numeric agreement (% = fraction of all 344 overlap timeslices)

| Columns | Bit-exact | Timeslices >5% diff |
|---|---|---|
| `IP`,`BT`,`RGEO`,`KAPPA`,`Q95`,`BEILI2`,`WMHD` | 87–91% | 0 |
| `NEL`,`PL`,`PLTH` | 66–85% | 8, 27, 23 respectively. |
| `TAUMHD`,`TAUDIA` | 31–35% | recompute-level (~52% sit at 0.01–1%) |
| `WFPAR`,`WFPER` | — | 2008 mostly blank; h-mode better coverage |

Shots driving the `NEL`/`PL`/`PLTH` >5% diffs: JET (all 8) 40943, 41679, 41687, 41738, 41740, 41755, 41851, 41852; JFT2M (9 largest of 22) 37520, 37544, 44963, 45147, 45203, 45260, 45716, 45737, 45739.

### 3e. Precedence: `LUPDATE` vs `LCUPDATE`, 344 overlap timeslices

| TOK | h-mode newer | 2008 newer |
|---|---|---|
| ASDEX / AUG / JET | 317 | 0 |
| JFT2M | 0 | 27 |

### 3f. Selection flags

| Flag | DB | True on overlap timeslices | Note |
|---|---|---|---|
| `SELEC2007` | 2008 | 0/344 | =1 for 1024/7700 timeslices (13.3%) file-wide; none of the 344 shared are selected |
| `SELDB5` | h-mode | 187/344 | true only when PHASE = H in both datasets (187/222); 0/122 in LH or OHM |

## 4. Sentinel (numeric "no data" placeholder) values

The sentinel values tend to be machine-specific, not column- or DB-specific.
The same columns can mix blank and sentinel, e.g. 2008 `NELEDGE` = 5374 blank + 1227 CMOD sentinel + 1099 real.

### 4a. `2008_data.csv`

| Literal | Meaning | Cells | Cols | Machine |
|---|---|---|---|---|
| `-9.999E-09` | float "no data" | 66,930 | 80 | CMOD 1227 + NSTX 9 (1236 timeslices) |
| `-9999999` | int "no data" | 14,869 | 14 | all CMOD (same 1227 timeslices) |
| `1.7E+38` | unknown | 399 | 7 | all CMOD (203 timeslices) |
| `-1.29E-31` | unknown | 226 | 2 (`PDIV`,`PMAIN`) | all AUG |
| `9999999` | sign-flip of int sentinel | 1 | 1 (`BSOURCE`) | — |

### 4b. `h-mode_data_V5.csv`

| Literal | Cells | Cols | Machine |
|---|---|---|---|
| `-9999999` | 1650 | 2 (`TPI`,`INDENT`) | all JET (825 each) |
| `0.9429` / `-0.9429` in `SPIN` | 1147 / 79 | 1 | all TEXTOR (suspect constant fill) |

Missing is otherwise encoded as `0` or blank, not the `-9.999E-09` scheme.

### 4c. `tc26_data.csv`

| Literal | Cells | Cols | Machine |
|---|---|---|---|
| `-` (dash) | 199 / 15 | `ZEFF` / `PRADCORE` | — |
| `-1.00E-08` | 52 | `FRACNMIN` | all CMOD |

## 5. String junk, whitespace, case, dead columns

### 5a. Junk string tokens

| DB | Token(s) | Variables | Total junk cells |
|---|---|---|---|
| 2008 | `?`-strings (`????????`/`??????????`/`???????`), `HELM?`, `Unknown` | `CAUSE`, `ECHLOC`, `ECHMODE`, `ICANTEN`, `ICANTEN2`, `ICLOC`, `ICLOC2`, `ICSCHEME`, `ICSCHEM2`, `ISEQ`, `PHASE`, `DIVNAMEnew` | 14036 |
| h-mode | `UNKNOWN`, `NA`, `?`-strings (`H???`/`HGELM???`/`H/SFE/??`/…) | `HYBRID`, `ITB`, `ITBTYPE`, `ELMTYPE`, `ICSCHEME`, `ICANTEN`, `ECHLOC`, `ECHMODE`, `PHASE`, `ISEQ` | 38796 |
| tc26 | `-` | `ZEFF`, `PRADCORE` | 214 |

### 5b. Whitespace and case (counts = affected cells)

| Type | Detail |
|---|---|
| 2008 whitespace | `WALMAT` 5239, `EVAP` 5229, `DIVMAT` 5224, `LIMMAT` 4639, `XTRGT` 4625, `DIVNAME` 4119, `ISEQ` 2426 |
| tc26 whitespace | 77 per column in `PHASE`,`CONFIG`,`WALMAT`,`DIVMAT`,`LIMMAT`,`DIVNAME`,`DIVCON`; `TOK` 25 |
| 2008 case | `AUXHEAT` NONE/none, NB/nb; `EVAP` NONE/None, BORO/boro; `PHASE` HSELMA/HSElMA; `WALMAT` SS/ss; `LIMMAT` C/c, MO/Mo |

h-mode and the tc26 are, as far as we know, otherwise clean apart from the tc26 `TOK` case above.

### 5c. Fully-dead columns (zero real data)

| DB | Dead columns |
|---|---|
| 2008 | `BGASA3`, `BGASZ3`, `IBFREQ`, `LHFREQ2`, `LHNPAR2`, `VPO95` (6) |
| h-mode | `ITBTYPE`, `FBS`, `RHOQ2`, `WROT`, `OMGAIMP0/H`, `OMGAM0/H`, `VTORV`, `VTORIMP`, `PMAIN`, `PDIV`, `GP_MAIN`, `GP_DIV` (14) |
| tc26 | none |

h-mode `SEPLIM` machine fills: TEXTOR all 1435 = `0.000`, TFTR all 104 = `0.000`, PDX all 143 = `0.180`, D3D 5 negative (min −0.45 m); 1556 exact-`0` total.

## 6. Normalization plan for migration

| Action | Targets |
|---|---|
| Map → empty/NaN | `-9.999E-09`, `-9999999`, `9999999`, `1.7E+38`, `-1.29E-31` (2008); `-9999999` (h-mode); `-`, `-1.00E-08` (tc26); all `?`-tokens; `Unknown`/`UNKNOWN`/`NA`/`is missing` |
| Crosswalk discard | fully-dead columns (5c) |
| TBD | |

## 7. Design decisions

Future work: Merge tool, locate duplicates, apply a strategy (most-recent / average / add-slice), decoupled from per-DB mapping

## 8. `TAUTH` empty cells for some selected rows in h-mode, filled from `TAUTH2`/`TAUTH1`

`TAUTH` (thermal energy confinement time, maps to `summary/global_quantities/tau_energy`) is blank in 328 timeslices, mostly ASDEX and AUG:

| Machine | blank `TAUTH` cells | of which another τ column is filled in |
|---|---|---|
| ASDEX | 210 | 210 |
| AUG | 70 | 70 |
| JET | 43 | 43 |
| DIII-D | 5 | 0 |

14 of AUG's 70 fall inside the STD5+ELMy subset used in `hmode_analysis.ipynb`.

Every blank row has one or more of `TAUTH1`, `TAUTH2`, `TAUMHD`, `TAUDIA` filled in instead, but only two of those four measure the same thing as `TAUTH`:

| Column | What it measures | Same quantity as `TAUTH`? |
|---|---|---|
| `TAUTH1` | thermal τ_E from kinetic profiles | yes, but blank in all 14 AUG rows |
| `TAUTH2` | thermal τ_E from total stored energy minus fast-ion energy | yes, filled in all 14 AUG rows |
| `TAUMHD` | *total* τ_E (fast-ion energy included, loss channels not subtracted) | no, different quantity |
| `TAUDIA` | *total* τ_E, diamagnetic version | no, different quantity |

`hmode_analysis.ipynb` falls back to `TAUTH2`, then `TAUTH1`. Compare to paper Table 1's per-machine STD5+ELMy counts: 

AUG was off by −11 (2133 vs. paper AUG+AUG-W 2144) with the 14 gaps left blank, vs. only +3 (2147) once they're filled from `TAUTH2`. `TAUTH1` is a second fallback for completeness.
