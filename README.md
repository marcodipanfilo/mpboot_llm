# MPBoot_LLM

MPBoot_LLM generates R2RML mappings from relational database dumps and target ontologies with an LLM-driven pipeline. The repo also includes a local evaluation stack for [RODI](https://www.semantic-web-journal.net/system/files/swj1439.pdf)-style benchmarks and shared result webpages.

## Requirements

- Python 3.9+
- Java 11+
- `git`
- `curl`
- `unzip`
- Docker
- either `mvn` or Docker access to the `maven` image

All commands below assume you are at the repository root:

```bash
cd mpboot_llm
```

## Environment Setup

Bootstrap the local toolchain:

```bash
bash scripts/bootstrap.sh
```

This installs or prepares:

- `.venv`
- `.tools/robot`
- `.tools/rodi`
- `.tools/ontop/jdbc`
- `.tools/bin/psql_docker.sh`
- the local PostgreSQL Docker container

Then create `.env`:

```bash
cp .env.example .env
```

At minimum, set the API key for the provider you want to use. The active provider/model is configured in [src/config/llm_config.py](src/config/llm_config.py).

Useful environment variables:

- `ANTHROPIC_API_KEY`
- `ANTHROPIC_PROXY_URL`
- `ANTHROPIC_MOCK_LOG_LEVEL`
- `MPBOOT_DB_PORT`
- `MPBOOT_DB_NAME`
- `MPBOOT_R2RML_FORCE_DOUBLE_FOR_DECIMALS`

## Workflow 1: Full RODI Pipeline

This is the workflow for the bundled RODI benchmark datasets under `datasets/rodi/`.

### One command for one dataset

Run everything for one dataset:

```bash
bash scripts/run_end_to_end_dataset.sh mondial_rel --method all
```

Variants:

```bash
bash scripts/run_end_to_end_dataset.sh mondial_rel --method rodi
bash scripts/run_end_to_end_dataset.sh mondial_rel --skip-evaluation --skip-summary
```

This wrapper will:

1. bootstrap missing tools
2. download the requested RODI dataset if missing
3. normalize `mondial_rel` if needed
4. build the PostgreSQL-compatible dataset copy
5. generate `ontology.owl` if needed
6. start the local Anthropic cache server
7. run mapping generation
8. stop the cache server
9. run evaluation
10. regenerate the shared summary webpages

### One command for all datasets

Run the full batch:

```bash
bash scripts/run_end_to_end_all.sh
```

Variants:

```bash
bash scripts/run_end_to_end_all.sh --method rodi
bash scripts/run_end_to_end_all.sh --skip-evaluation --skip-summary
```

### Manual step-by-step RODI workflow

If you want the individual steps instead of the wrapper:

1. Download the selected benchmark datasets:

```bash
bash scripts/bootstrap.sh --download-rodi
```

Download only one dataset:

```bash
bash scripts/bootstrap.sh --download-rodi --dataset mondial_rel
```

2. Build PostgreSQL-compatible dataset copies:

```bash
bash scripts/create_pg_compatible_dataset.sh datasets/rodi
```

3. Generate OWL/XML where needed:

```bash
bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible
```

4. Run mapping generation:

```bash
bash scripts/create_all_mapping.sh pg_compatible/outputs/data_pg_compatible --keep-going
```

Or one dataset only:

```bash
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/mondial_rel
```

5. Evaluate an archived batch:

```bash
bash scripts/evaluation.sh outputs/<model>/<timestamp> --method all --keep-going
```

Only one dataset:

```bash
bash scripts/evaluation.sh outputs/<model>/<timestamp> --dataset mondial_rel --method all
```

6. Regenerate the shared webpages:

```bash
bash scripts/generate_summary_portal.sh
```

You can still pass an archived batch path if you want to anchor discovery to one run:

```bash
bash scripts/generate_summary_portal.sh outputs/<model>/<timestamp>
```

### Notes about `mondial_rel`

`mondial_rel` needs one repo-specific normalization step. During bootstrap preparation, the repo:

- keeps only the relevant schema from the original dump
- renames the schema to `mondial_rel`
- strips obsolete schema prefixes from the Mondial `.qpair` SQL

That normalization is handled by [scripts/bootstrap_prepare_rodi_dumps.sh](scripts/bootstrap_prepare_rodi_dumps.sh).

## Workflow 2: Generate Mappings From Your Own Dump And Ontology

If you already have a relational dump and an ontology, you do not need the RODI dataset download path. The mapping pipeline expects a dataset directory containing:

- `dump.sql` or `dump_pg_compatible.sql`
- `ontology.ttl` or `ontology.owl`
- optionally `queries/*.qpair` if you also want RODI-style evaluation later

### Minimal directory layout

Example:

```text
my_input/
  my_dataset/
    dump.sql
    ontology.ttl
```

### Convert your dataset to the repo's PostgreSQL-compatible format

```bash
bash scripts/create_pg_compatible_dataset.sh my_input pg_compatible/outputs/data_pg_compatible
```

That will create:

```text
pg_compatible/outputs/data_pg_compatible/my_dataset/
```

with:

- `dump_pg_compatible.sql`
- `ontology.ttl` or copied `ontology.owl`
- copied extra files such as `queries/`

If you only want to process a single dataset directory directly:

```bash
bash scripts/create_pg_compatible_dataset.sh my_input/my_dataset pg_compatible/outputs/data_pg_compatible/my_dataset
```

### Generate `ontology.owl` if your ontology is Turtle

```bash
bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible --dataset my_dataset
```

If `ontology.owl` already exists, this step can be skipped unless you want to overwrite it:

```bash
bash scripts/generate_owlxml_ontologies.sh pg_compatible/outputs/data_pg_compatible --dataset my_dataset --overwrite
```

### Run mapping generation for that dataset

```bash
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/my_dataset
```

The runner will stage the dataset into the live workspace:

- [src/inputs/database](src/inputs/database)
- [src/inputs/ontology](src/inputs/ontology)

and then execute the mapping phases.

Useful variants:

```bash
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/my_dataset --dry-run
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/my_dataset --from phase1
bash scripts/create_mapping_single_dataset.sh --dataset-dir pg_compatible/outputs/data_pg_compatible/my_dataset --only phase7
```

### Evaluate your own dataset

Evaluation is optional. If you also provide `queries/*.qpair`, you can evaluate an archived run later with:

```bash
bash scripts/evaluation.sh outputs/<model>/<timestamp> --dataset my_dataset --method all
```

If you have no qpair queries, the mapping-generation workflow still works; only the RODI query evaluation path is unavailable.

## Outputs

### Live workspace during a run

The active workspace used by the mapping agents is:

- [src/inputs](src/inputs)
- [src/outputs](src/outputs)
- [src/memory](src/memory)

The live generated mapping ends up at:

- [src/outputs/mappings/mappings_r2rml.ttl](src/outputs/mappings/mappings_r2rml.ttl)

### Archived runs

Each completed run is archived under:

```text
outputs/<model>/<timestamp>/<dataset>/
```

Typical contents:

- `mappings_r2rml.ttl`
- `run_metadata.json`
- `run.log`
- `inputs/`
- `workspace/`
- `evaluation/` after evaluation

### Shared summary webpages

Generated result pages live under:

- [outputs/summary/index.html](outputs/summary/index.html)
- [outputs/summary/rodi_f1_site_refactored/index.html](outputs/summary/rodi_f1_site_refactored/index.html)
- [outputs/summary/summary_table_site/index.html](outputs/summary/summary_table_site/index.html)

Regenerate them with:

```bash
bash scripts/generate_summary_portal.sh
```

## Script Reference

Main entrypoints:

- [scripts/bootstrap.sh](scripts/bootstrap.sh)
- [scripts/run_end_to_end_dataset.sh](scripts/run_end_to_end_dataset.sh)
- [scripts/run_end_to_end_all.sh](scripts/run_end_to_end_all.sh)
- [scripts/create_pg_compatible_dataset.sh](scripts/create_pg_compatible_dataset.sh)
- [scripts/generate_owlxml_ontologies.sh](scripts/generate_owlxml_ontologies.sh)
- [scripts/create_mapping_single_dataset.sh](scripts/create_mapping_single_dataset.sh)
- [scripts/create_all_mapping.sh](scripts/create_all_mapping.sh)
- [scripts/evaluation.sh](scripts/evaluation.sh)
- [scripts/generate_summary_portal.sh](scripts/generate_summary_portal.sh)
- [scripts/start_anthropic_mock_server.sh](scripts/start_anthropic_mock_server.sh)

# Reference metadata

[![DOI](https://zenodo.org/badge/1232011513.svg)](https://doi.org/10.5281/zenodo.20073239)

This repository is archived on Zenodo at [10.5281/zenodo.20073239](https://doi.org/10.5281/zenodo.20073239).

