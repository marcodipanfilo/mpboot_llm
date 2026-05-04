# LLM_MPBoot

LLM_MPBoot is a pipeline for generating ontology mappings from relational database dumps and target ontologies using LLMs.

The current workflow is script-driven:
- bootstrap the environment
- optionally download the selected RODI datasets
- generate PostgreSQL-compatible dataset copies
- generate OWL/XML ontology files where needed
- run the mapping pipeline for one dataset or for a full batch
- evaluate archived mapping runs with RODI and/or Ontop

## Requirements

- Python 3.9+
- Java 11+ for ROBOT, Ontop, and RODI
- `curl`
- `git`
- `unzip`
- Docker
- either `mvn` or Docker access to the `maven` image

## Main scripts

All commands below assume you are at the repository root:

```bash
cd mpboot_llm
```

### 1. Bootstrap

Create the virtual environment, install Python dependencies, and install the local evaluation/tooling stack:

```bash
bash scripts/bootstrap.sh
```

This bootstrap now also:

- installs ROBOT under `.tools/robot/`
- installs Ontop CLI under `.tools/ontop/`
- clones and builds RODI under `.tools/rodi/`
- installs the PostgreSQL JDBC driver under `.tools/ontop/jdbc/`
- starts a PostgreSQL 11 Docker container named `mpboot-postgres`
- creates a repo-local `psql` wrapper at `.tools/bin/psql_docker.sh`

The Python evaluation dependencies used by `scripts/evaluation.sh`, including `requests` and `psycopg2-binary`, are also installed here.

The bootstrap logic is split into smaller scripts under `scripts/` and orchestrated by `scripts/bootstrap.sh`:

- `bootstrap_python_env.sh`
- `bootstrap_robot.sh`
- `bootstrap_ontop.sh`
- `bootstrap_jdbc.sh`
- `bootstrap_rodi.sh`
- `bootstrap_postgres.sh`
- `bootstrap_psql_wrapper.sh`
- `bootstrap_download_rodi_datasets.sh`
- `bootstrap_prepare_rodi_dumps.sh`
- `start_anthropic_mock_server.sh`

To also download the selected RODI benchmark datasets into `datasets/rodi/`:

```bash
bash scripts/bootstrap.sh --download-rodi
```

To download only one benchmark dataset during bootstrap:

```bash
bash scripts/bootstrap.sh --download-rodi --dataset mondial_rel
```

The bootstrap dataset download is intentionally limited to this fixed subset:

- `cmt_denormalized`
- `cmt_renamed`
- `cmt_structured`
- `conference_nofks`
- `conference_renamed`
- `conference_structured`
- `mondial_rel`
- `npd_atomic_tests`
- `sigkdd_mixed`
- `sigkdd_renamed`
- `sigkdd_structured`

When `--download-rodi` is used, the script deletes the existing `datasets/rodi/` directory first and then repopulates it.

`mondial_rel` also receives a repo-specific preparation step during bootstrap. Its original RODI dump contains two schemas, but the mapping pipeline currently assumes one schema per dataset dump. The bootstrap preparation step rewrites:

```text
datasets/rodi/mondial_rel/dump.sql
```

so that it keeps only the `mondial_rdf2sql_standard` schema and discards the `mondial_rel` schema before PostgreSQL-compatible dataset generation.

### 2. Generate PostgreSQL-compatible datasets

Build the PostgreSQL-compatible dataset tree from `datasets/rodi/`:

```bash
bash scripts/create_pg_compatible_dataset.sh datasets/rodi
```

This writes to:

```text
pg_compatible/outputs/data_pg_compatible/
```

For `mondial_rel`, this step runs on the already prepared single-schema dump produced during bootstrap, so the pg-compatible dataset is generated only from `mondial_rdf2sql_standard`.

During later RODI evaluation, the runner also applies a repo-side compatibility patch to the temporary RODI mapping copy for `mondial_rel`: it prefixes the generated mapping SQL with the actual dump schema name. This is necessary because local RODI hardcodes `SET SEARCH_PATH TO <scenario>` and would otherwise fail when the prepared dump schema name differs from the scenario name.

### 3. Generate OWL/XML ontology files

Some datasets provide `ontology.ttl` instead of `ontology.owl`. Generate `ontology.owl` files in OWL/XML syntax with ROBOT:

```bash
bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible
```

To do this for a single dataset only:

```bash
bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible --dataset mondial_rel
```

To overwrite already existing `ontology.owl` files:

```bash
bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible --overwrite
```

This step does two things:

- runs ROBOT to convert `ontology.ttl` to OWL/XML `ontology.owl`
- applies a repo-side normalization pass afterward

That normalization is especially important for `mondial_rel`. Its generated OWL/XML is rewritten so it matches the structure used by the other generated ontologies:

- `ontologyIRI` is set explicitly
- `xml:base` is set to the ontology base IRI
- the default empty prefix is added
- in-ontology absolute IRIs such as `IRI="http://...#AdministrativeSubdivision"` are rewritten to local IRIs such as `IRI="#AdministrativeSubdivision"`

### 4. Configure the LLM

Copy the example environment file:

```bash
cp .env.example .env
```

Fill in the API key you want to use, for example:

```text
ANTHROPIC_API_KEY=your_real_key_here
```

The provider and model mapping are currently selected in:

- [src/config/llm_config.py](src/config/llm_config.py)

The two main settings are:

- `SELECTED_PROVIDER`
- `MODELS`

### 5. Run mappings for a single dataset

Run the mapping pipeline for one staged dataset:

```bash
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/mondial_rel
```

Dry-run only:

```bash
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/mondial_rel --dry-run
```

Resume from a specific phase:

```bash
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/mondial_rel --from phase1
```

Run only one phase:

```bash
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/mondial_rel --only phase7
```

### 5b. Run one dataset end to end

To go from bootstrap prerequisites to archived mapping outputs, evaluation, and regenerated summary webpages in one command:

```bash
bash scripts/run_end_to_end_dataset.sh mondial_rel
```

This wrapper:

- bootstraps missing tools only when needed
- downloads only the requested RODI dataset when it is missing
- rebuilds only that dataset’s PostgreSQL-compatible copy
- regenerates only that dataset’s OWL/XML ontology
- starts the Anthropic cache server for the mapping phase and stops it afterward
- archives a fresh mapping batch for that dataset
- runs evaluation for that dataset
- regenerates the shared summary webpages under `outputs/summary/`

Use a different evaluation mode if needed:

```bash
bash scripts/run_end_to_end_dataset.sh mondial_rel --method rodi
```

Stop after mapping generation:

```bash
bash scripts/run_end_to_end_dataset.sh mondial_rel --skip-evaluation --skip-summary
```

To start the Anthropic cache/mock server manually:

```bash
bash scripts/start_anthropic_mock_server.sh
```

For example, to run it in replay mode on a different port:

```bash
bash scripts/start_anthropic_mock_server.sh --mode replay --port 8001
```

### 6. Run mappings for all datasets

Run the full batch:

```bash
bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible
```

Dry-run the full batch:

```bash
bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --dry-run
```

Run one dataset only through the batch runner:

```bash
bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --dataset mondial_rel
```

Continue after failures:

```bash
bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --keep-going
```

Resume each dataset from a specific phase:

```bash
bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --from phase1
```

### 7. Evaluate archived runs

The evaluation runner works on archived dataset outputs under:

```text
outputs/<model>/<batch_timestamp>/<dataset>/
```

It supports two methods imported from the `mapping-strategy_fixes` work:

- `rodi`
- `ontop`

Run both methods for a whole archived batch:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> --method all
```

Run only one dataset:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> \
  --dataset mondial_rel \
  --method all
```

Run only the Ontop-style evaluation:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> \
  --dataset mondial_rel \
  --method ontop
```

Run only the RODI evaluation:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> \
  --dataset mondial_rel \
  --method rodi
```

For `mondial_rel`, the RODI evaluation path also applies two evaluation-time compatibility workarounds to the temporary RODI copy only:

- it prefixes mapping SQL with the dump-derived schema where needed
- it adds the JVM flag `-Djava.util.Arrays.useLegacyMergeSort=true` to avoid an old OWLAPI/RODI reasoning-time comparator crash during RDF/XML serialization

These adjustments do not modify the archived mapping files under `outputs/...`.

For Ontop, the evaluation path also applies temporary method-specific compatibility fixes without modifying the archived root mapping:

- it patches the temporary Ontop mapping copy when ontology-declared XSD datatypes are missing or mismatched in the generated mapping
- it generates Ontop-specific intermediate files only under `evaluation/ontop/`

Known remaining issue classes are intentionally handled outside the archived root mapping:

- object properties incorrectly emitted as literal/data-property mappings
- mappings whose logical table/query is invalid against the current dump

Use the helper scripts below before a large Ontop batch if you want to sanitize archived mappings explicitly:

```bash
bash scripts/check_ontop_mapping_mismatches.sh outputs/<model>/<batch_timestamp> --remove
bash scripts/check_mapping_database_validity.sh outputs/<model>/<batch_timestamp> --remove
```

Compare the two tabular reports after running both:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> \
  --dataset mondial_rel \
  --method all \
  --compare-tabular
```

The evaluation runner uses these repo-local defaults unless you override them:

- RODI root: `.tools/rodi`
- Ontop dir: `.tools/ontop`
- host: `localhost`
- port: `5433`
- database: `rodi`
- user: `postgres`
- password: `postgres`
- psql command: `.tools/bin/psql_docker.sh`

Database preparation modes:

- `--db-setup auto`
  - default
  - uses RODI setup when the dataset exists in the local RODI benchmark checkout
  - otherwise imports the archived `inputs/dump.sql`
- `--db-setup rodi`
  - prepares the DB using the local RODI benchmark scenario setup
- `--db-setup dump`
  - imports the archived `inputs/dump.sql` into PostgreSQL before Ontop evaluation
- `--db-setup none`
  - assumes the database is already prepared

You can override the runtime with flags such as `--rodi-root`, `--ontop-dir`, `--db-host`, `--db-port`, `--db-name`, `--db-user`, `--db-password`, and `--db-cmd`.

## Outputs

### Live workspace during a run

The pipeline writes its live intermediate files to:

- `src/inputs/`
- `src/outputs/`
- `src/memory/`

The final generated mapping file in the live workspace is:

```text
src/outputs/mappings/mappings_r2rml.ttl
```

### Archived outputs for batch runs

The batch runner archives each dataset run under:

```text
outputs/<model>/<batch_timestamp>/<dataset>/
```

Example:

```text
outputs/claude-haiku-4-5-20251001/20260428_110804_786765/mondial_rel/
```

Each archived dataset directory contains:

- `mappings_r2rml.ttl`
- `run_metadata.json`
- `run.log`
- `evaluation/` after an evaluation run
- `inputs/`
- `workspace/`

The complete workspace snapshot is preserved under:

```text
outputs/<model>/<batch_timestamp>/<dataset>/workspace/
```

That includes:

- `workspace/inputs/`
- `workspace/outputs/`
- `workspace/memory/`

### Logs

For real batch runs, the stdout/stderr stream for each dataset is saved to:

```text
outputs/<model>/<batch_timestamp>/<dataset>/run.log
```

Dry-runs do not create archived files or logs.

### Evaluation artifacts

After running `scripts/evaluation.sh`, each evaluated dataset directory contains:

- `evaluation/evaluation.log` for combined `--method all` runs
- `evaluation/rodi/evaluation.log`
- `evaluation/rodi/mappings__r2rml_rodi_patch.ttl`
- `evaluation/rodi/eval_rodi__report.txt`
- `evaluation/rodi/eval_rodi__tabular.txt`
- `evaluation/ontop/evaluation.log`
- `evaluation/ontop/mappings__r2rml_ontop_patch.ttl` when an Ontop-only patch is needed
- `evaluation/ontop/mappings__r2rml.obda`
- `evaluation/ontop/config__ontop_db.properties`
- `evaluation/ontop/ontop_endpoint.log`
- `evaluation/ontop/eval_ontop__metrics.json`
- `evaluation/ontop/eval_ontop__summary.txt`
- `evaluation/ontop/eval_ontop__tabular.txt`
- `evaluation/compare/eval_compare__tabular.diff` when `--compare-tabular` detects a mismatch

### Generated summary webpages

The repo can also generate interactive result pages under the shared top-level summary directory:

```text
outputs/summary/
```

Current generated pages:

- `outputs/summary/index.html`
  - shared entry page / portal
  - switches between the detailed matrix and the grouped summary table
- `outputs/summary/rodi_f1_site_refactored/index.html`
  - interactive F1 matrix across runs and methods
  - supports source selection, suffixes, filters, sorting, and comparison
- `outputs/summary/summary_table_site/index.html`
  - paper-style grouped summary table
  - includes built-in paper baselines such as `D2RQ`, `MIRR.`, `ontop`, `COMA`, `IncM.`, `B.OX`, and `LLM4VKG -paper`

Generate the detailed matrix page:

```bash
python src/evaluation/generate_rodi_f1_site_refactored.py \
  outputs/<model>/<batch_timestamp>
```

Generate the grouped summary table:

```bash
bash scripts/generate_summary_table_site.sh \
  outputs/<model>/<batch_timestamp>
```

Generate the shared portal page:

```bash
bash scripts/generate_summary_portal.sh \
  outputs/<model>/<batch_timestamp>
```

The current default output location for all three is the shared root:

```text
outputs/summary/
```

so the generated webpages are not tied to one archived batch path anymore.

## Known evaluation issues

The current benchmarks expose several recurring compatibility problems. The repo now contains targeted countermeasures for them:

- `mondial_rel` dump shape:
  - bootstrap keeps only schema `mondial_rdf2sql_standard`
  - during RODI evaluation, the temporary RODI mapping copy is schema-qualified because local RODI forces `SET SEARCH_PATH TO <scenario>`
- ROBOT OWL/XML output:
  - `scripts/generate_owlxml_ontologies.sh` normalizes generated `ontology.owl` files, especially for `mondial_rel`
- RODI datatype/runtime issues:
  - the temporary RODI mapping copy rewrites unsupported datatypes such as `xsd:anyURI`, `xsd:nonNegativeInteger`, `xsd:positiveInteger`, `xsd:unsignedLong`, and `xsd:unsignedInt`
  - RODI is launched with `-Djava.util.Arrays.useLegacyMergeSort=true` to avoid the old OWLAPI comparator crash during reasoning
- Ontop datatype mismatches:
  - the temporary Ontop mapping copy restores ontology-compatible XSD datatypes such as `xsd:decimal`
  - missing literal datatypes are inserted when the ontology declares an XSD datatype range
- Ontop object-property mismatches:
  - use `scripts/check_ontop_mapping_mismatches.sh` to comment out generated literal mappings for ontology object properties
- Mapping/DB validity issues:
  - use `scripts/check_mapping_database_validity.sh` to comment out triples maps whose SQL/table/column usage is invalid against the archived dump

## Full run

End-to-end full batch run:

```bash
bash scripts/bootstrap.sh --download-rodi
bash scripts/create_pg_compatible_dataset.sh datasets/rodi
bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible --overwrite
bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --force-all --keep-going
```

After the mapping batch finishes, evaluate the archived run with Ontop first:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> --method ontop --force-all --keep-going
```

If needed, sanitize archived mappings before or between evaluation runs:

```bash
bash scripts/check_ontop_mapping_mismatches.sh outputs/<model>/<batch_timestamp> --remove
bash scripts/check_mapping_database_validity.sh outputs/<model>/<batch_timestamp> --remove
```

Then run the RODI evaluation:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> --method rodi --force-all --keep-going
```

Finally, if both method outputs exist, compare the tabular reports:

```bash
bash scripts/evaluation.sh outputs/<model>/<batch_timestamp> --method all --compare-tabular
```

You can then build the interactive webpages from that evaluated batch:

```bash
python src/evaluation/generate_rodi_f1_site_refactored.py \
  outputs/<model>/<batch_timestamp>
bash scripts/generate_summary_table_site.sh \
  outputs/<model>/<batch_timestamp>
bash scripts/generate_summary_portal.sh \
  outputs/<model>/<batch_timestamp>
```

## Script summary

- [scripts/bootstrap.sh](scripts/bootstrap.sh)
  - orchestrates the full bootstrap flow
  - optionally downloads the selected RODI datasets

- `scripts/_bootstrap_common.sh`
  - shared bootstrap variables and helper functions

- `scripts/bootstrap_python_env.sh`
  - creates `.venv` and installs Python dependencies

- `scripts/bootstrap_robot.sh`
  - installs ROBOT

- `scripts/bootstrap_ontop.sh`
  - installs Ontop CLI

- `scripts/bootstrap_jdbc.sh`
  - installs the PostgreSQL JDBC driver into Ontop's `jdbc/` directory

- `scripts/bootstrap_rodi.sh`
  - clones and builds RODI

- `scripts/bootstrap_postgres.sh`
  - starts Docker PostgreSQL and ensures the target database exists

- `scripts/bootstrap_psql_wrapper.sh`
  - creates the repo-local `psql` wrapper used by evaluation

- `scripts/bootstrap_download_rodi_datasets.sh`
  - downloads the selected RODI datasets into `datasets/rodi`

- `scripts/bootstrap_prepare_rodi_dumps.sh`
  - applies repo-specific dump preparation after bootstrap
  - currently rewrites `datasets/rodi/mondial_rel/dump.sql` to keep only `mondial_rdf2sql_standard`

- [scripts/create_pg_compatible_dataset.sh](scripts/create_pg_compatible_dataset.sh)
  - builds PostgreSQL-compatible dataset copies

- [scripts/generate_owlxml_ontologies.sh](scripts/generate_owlxml_ontologies.sh)
  - generates `ontology.owl` files from `ontology.ttl` using ROBOT
  - normalizes the generated OWL/XML afterward when needed, including the `mondial_rel` ontology fixup

- [scripts/create_mapping_single_dataset.sh](scripts/create_mapping_single_dataset.sh)
  - runs the mapping pipeline for one dataset

- [scripts/create_all_mapping.sh](scripts/create_all_mapping.sh)
  - runs the mapping pipeline for a dataset batch and archives outputs

- [scripts/evaluation.sh](scripts/evaluation.sh)
  - evaluates archived mapping runs with RODI and/or Ontop
  - for `mondial_rel`, the RODI path additionally patches the temporary RODI mapping SQL with dump-derived schema prefixes and uses the legacy Java mergesort workaround during reasoning

- [scripts/generate_rodi_paper_table.sh](scripts/generate_rodi_paper_table.sh)
  - generates static markdown / CSV / LaTeX paper-style summary tables from RODI results

- [scripts/generate_summary_table_site.sh](scripts/generate_summary_table_site.sh)
  - generates the interactive grouped summary table webpage under `outputs/summary/summary_table_site/`

- [scripts/generate_summary_portal.sh](scripts/generate_summary_portal.sh)
  - generates the shared summary portal under `outputs/summary/index.html`

- `src/evaluation/generate_rodi_f1_site_refactored.py`
  - generates the interactive detailed F1 matrix webpage under `outputs/summary/rodi_f1_site_refactored/`

## Current notes

- The mapping phases still operate through the existing phase scripts under `src/agents/` and `src/parsers/`.
- The single-dataset and batch orchestration logic now lives under `src/runners/`.
- The bootstrapper now prepares the default local RODI, Ontop, JDBC, and Docker PostgreSQL setup used by the evaluation runner.
