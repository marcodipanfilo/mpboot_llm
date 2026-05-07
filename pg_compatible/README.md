# pg_compatible

A standalone module for transforming relational benchmark datasets into PostgreSQL-compatible form and validating that the transformed version is semantically equivalent to the original one.

This module was designed primarily for benchmark-style datasets such as RODI, but it is intentionally kept independent from `vanilla_llm` so it can be reused by other modules and pipelines.

## What this module does

The module provides a complete workflow to:

- normalize SQL dumps so they load cleanly in PostgreSQL
- transform SQL queries stored in `.qpair` files so they match the transformed schema
- mirror a full dataset tree into a PostgreSQL-compatible version
- regression-test original vs transformed datasets by loading both into PostgreSQL and comparing schemas and query results

The main motivation is to remove dependence on quoted identifiers and inconsistent casing while preserving the meaning of the original benchmark.

## Folder structure

A typical structure looks like this:

```text
pg_compatible/
├── __init__.py
├── sanitizer.py
├── transform_dump.py
├── transform_qpair.py
├── transform_qpair_dir.py
├── build_dataset.py
├── regression_test.py
├── outputs/
│   ├── data_pg_compatible/
│   └── regression_artifacts/
└── README.md
```

## Files and responsibilities

### `sanitizer.py`

Contains the core identifier normalization logic.

Responsibilities:
- normalize quoted identifiers to PostgreSQL-safe unquoted identifiers
- preserve real data values and string literals
- track renamed identifiers and detect collisions

This is the core shared component used by both dump and qpair transformation.

### `transform_dump.py`

Transforms a single `dump.sql` file into a PostgreSQL-compatible dump.

Typical transformation:
- `dump.sql` → `dump_pg_compatible.sql`

### `transform_qpair.py`

Transforms a single `.qpair` file.

Typical transformation:
- `Q05.qpair` → `Q05_pg_compatible.qpair`

### `transform_qpair_dir.py`

Transforms all `.qpair` files inside a directory.

Useful when you want to work only on a query folder instead of a full dataset tree.

### `build_dataset.py`

Builds a full mirrored PostgreSQL-compatible dataset tree.

It recursively walks the source dataset folder and:
- transforms every `dump.sql`
- transforms every `.qpair`
- copies all other files unchanged

### `regression_test.py`

Runs the full validation pipeline.

For each dataset, it:
- creates two temporary PostgreSQL databases
- loads the original dump into one
- loads the transformed dump into the other
- compares schemas structurally
- runs matching qpair SQL queries on both
- compares the results

It also:
- handles dataset-specific schemas via `search_path`
- ignores row-order differences unless `ORDER BY` is present
- skips qpair files marked as disabled via `disabled=...`

## Outputs

All generated outputs are meant to live inside the module itself:

```text
pg_compatible/outputs/
├── data_pg_compatible/
└── regression_artifacts/
```

### `outputs/data_pg_compatible/`

Contains the transformed datasets.

Example:

```text
pg_compatible/outputs/data_pg_compatible/cmt_naive/
├── dump_pg_compatible.sql
├── ontology.owl
└── queries/
    ├── Q01_pg_compatible.qpair
    └── ...
```

### `outputs/regression_artifacts/`

Contains intermediate files written by the regression test, such as:
- schema dumps
- raw query result CSV files
- normalized query result CSV files

These are helpful when debugging mismatches.

## Requirements

This module expects:
- Python 3.9+
- PostgreSQL reachable either locally or in Docker
- `uv` for convenient execution

## Running the tools

All commands below assume you are at the project root.

## 1. Build a full PostgreSQL-compatible dataset tree

### Default style

If `build_dataset.py` is configured to default to the module output directory, you can run:

```bash
uv run python pg_compatible/build_dataset.py rodi/data
```

This should write output to:

```text
pg_compatible/outputs/data_pg_compatible/
```

### Explicit output directory

```bash
uv run python pg_compatible/build_dataset.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible
```

## 2. Transform a single dump file

Useful for debugging one dataset only.

```bash
uv run python pg_compatible/transform_dump.py \
  rodi/data/cmt_naive/dump.sql
```

This creates:

```text
rodi/data/cmt_naive/dump_pg_compatible.sql
```

### Explicit output file

```bash
uv run python pg_compatible/transform_dump.py \
  rodi/data/cmt_naive/dump.sql \
  pg_compatible/outputs/tmp/cmt_naive_dump_pg_compatible.sql
```

## 3. Transform a single qpair file

```bash
uv run python pg_compatible/transform_qpair.py \
  rodi/data/cmt_naive/queries/Q01.qpair
```

This creates:

```text
rodi/data/cmt_naive/queries/Q01_pg_compatible.qpair
```

### Explicit output file

```bash
uv run python pg_compatible/transform_qpair.py \
  rodi/data/cmt_naive/queries/Q01.qpair \
  pg_compatible/outputs/tmp/Q01_pg_compatible.qpair
```

## 4. Transform a qpair directory

```bash
uv run python pg_compatible/transform_qpair_dir.py \
  rodi/data/cmt_naive/queries \
  pg_compatible/outputs/tmp/cmt_naive_queries_pg_compatible
```

## 5. Run the regression test in Docker mode

This is the recommended mode if PostgreSQL runs in Docker and `psql` is available inside the container.

Example using container `pg11-rodi`:

```bash
uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode docker \
  --container pg11-rodi \
  --user postgres \
  --password postgres
```

### Run regression on a single dataset only

```bash
uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode docker \
  --container pg11-rodi \
  --user postgres \
  --password postgres \
  --dataset cmt_naive
```

### Keep temporary databases for inspection

```bash
uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode docker \
  --container pg11-rodi \
  --user postgres \
  --password postgres \
  --dataset cmt_naive \
  --keep-dbs
```

This is useful if you want to inspect the generated databases later in pgAdmin.

## 6. Run the regression test in local mode

Use local mode if `psql` is installed on your machine and reachable on your `PATH`.

```bash
uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode local \
  --host localhost \
  --port 5433 \
  --user postgres \
  --password postgres
```

### Single dataset in local mode

```bash
uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode local \
  --host localhost \
  --port 5433 \
  --user postgres \
  --password postgres \
  --dataset cmt_naive
```

## 7. Run the regression test in auto mode

Auto mode tries:
- local `psql` first
- Docker mode if a container is supplied and local mode is unavailable

```bash
uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode auto \
  --container pg11-rodi \
  --user postgres \
  --password postgres
```

## What the regression test checks

For each dataset, the regression script checks the following.

### Schema equivalence

The script extracts a structural signature of the schema from PostgreSQL system views and compares:
- tables
- columns
- datatypes
- nullability
- defaults
- primary keys
- foreign keys

Comparison is done after normalization, so identifier case and formatting differences do not cause false mismatches.

### Query result equivalence

For each qpair:
- the original query is run on the original database
- the transformed query is run on the transformed database
- results are compared after normalization

If no `ORDER BY` is present, rows are sorted before comparison so semantically identical answers with different row order still match.

### Disabled qpair handling

If a qpair contains:

```text
disabled=...
```

it is skipped automatically. This is important because some benchmark qpair files are intentionally invalid.

## Typical workflow

### Full workflow in Docker mode

```bash
uv run python pg_compatible/build_dataset.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible

uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode docker \
  --container pg11-rodi \
  --user postgres \
  --password postgres
```

### Full workflow in local mode

```bash
uv run python pg_compatible/build_dataset.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible

uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode local \
  --host localhost \
  --port 5433 \
  --user postgres \
  --password postgres
```

## Expected final output

A successful full run ends with:

```text
ALL DATASETS PASSED
```

This means:
- transformed dumps load successfully
- transformed queries execute successfully
- schemas match structurally
- query results match semantically for all executable qpair files

## Notes on benchmark quirks

This module explicitly accounts for several benchmark-specific issues.

### Disabled queries

Some qpair files are marked as disabled because they are known invalid benchmark queries.
These are skipped rather than treated as failures.

### Inline SQL comments

Some qpair SQL strings contain `--` comments that make direct wrapping into `COPY (...)` problematic.
The regression runner sanitizes these enough for execution.

### Dataset-specific schemas

Some datasets are loaded into a schema named after the dataset rather than `public`.
The regression test sets `search_path` accordingly before query execution.

### Result ordering

Many benchmark queries do not specify an `ORDER BY`.
In those cases, result rows are sorted before comparison to avoid false mismatches caused by PostgreSQL returning rows in different orders.

## Suggested git handling

Since generated outputs can be large, it is usually a good idea to ignore them in git.

Example `.gitignore` entries:

```gitignore
pg_compatible/outputs/data_pg_compatible/
pg_compatible/outputs/regression_artifacts/
```

If you want to keep small samples or logs, store those separately.

## Example result logging

To save a full regression run log:

```bash
uv run python pg_compatible/regression_test.py \
  rodi/data \
  pg_compatible/outputs/data_pg_compatible \
  --mode docker \
  --container pg11-rodi \
  --user postgres \
  --password postgres | tee pg_compatible/outputs/regression_artifacts/regression.log
```

## Summary

This module gives you a reusable, benchmark-oriented PostgreSQL compatibility layer that:
- transforms datasets
- validates them rigorously
- is independent of `vanilla_llm`
- can be reused by other modules

It is especially useful when you want reproducible experiments over heterogeneous benchmark datasets whose original SQL is not PostgreSQL-friendly out of the box.

## `check_dataset_structure.py`

Audits and optionally fixes the structure of already generated PostgreSQL-compatible datasets.

This script is intended as a final validation and cleanup step for dataset collections produced by this module.

### Expected dataset structure

dataset_name/
├── dump_pg_compatible.sql
├── ontology.ttl
├── ontology.owl
└── queries/
    ├── Q01.qpair
    ├── Q02.qpair
    └── ...

### What the script checks

For each dataset folder, the script verifies:

- presence of `dump_pg_compatible.sql`
- presence of `ontology.ttl`
- presence of `ontology.owl`
- existence of `queries/`
- at least one `.qpair` file inside `queries/`

It also detects any files that do not belong to this structure.

### Modes

#### Audit mode

Only reports issues:

uv run --with rdflib python pg_compatible/check_dataset_structure.py <dir> --mode audit

#### Fix mode

Performs cleanup and normalization:

- deletes unexpected root files
- deletes unexpected files inside dataset folders
- converts ontology.ttl ↔ ontology.owl if needed
- deletes folders still invalid after fixing

uv run --with rdflib python pg_compatible/check_dataset_structure.py <dir> --mode fix

### Typical usage

uv run python pg_compatible/build_dataset.py rodi/data pg_compatible/outputs/data_pg_compatible

uv run --with rdflib python pg_compatible/check_dataset_structure.py pg_compatible/outputs/data_pg_compatible --mode fix

uv run python pg_compatible/regression_test.py rodi/data pg_compatible/outputs/data_pg_compatible --mode docker --container pg11-rodi --user postgres --password postgres
