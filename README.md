# itpa-imas-migration

This code was extracted from the migration workstream of a fork of IMAS `IDStools` (https://github.com/fionncur/IDStools)

## Layout

```
src/itpa_imas_migration/
    idsmigration.py        # the migration script (CSV -> summary IDS + SimDB manifest)
    generate_crosswalk.py  # authors/regenerates crosswalk workbooks with live DD lookups
docs/                      # migration.md (idsmigration reference), data logbook
resources/mappings/        # crosswalk workbooks (canonical) + csv/ projections
resources/input/           # source CSV databases (untracked)
notebooks/                 # analysis notebooks
```

## Installation

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows
pip install -e .
```

If `backports-datetime-fromisoformat` (pulled in by `imas-simdb`) fails to build run `pip install --no-deps imas-simdb` first,
then re-run the editable install.

The migration reference (crosswalk specification, SimDB ingestion) is in
[docs/migration.md](docs/migration.md).

## License

LGPL-3.0-or-later. See [LICENSE.md](LICENSE.md).
