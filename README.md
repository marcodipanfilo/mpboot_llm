# LLM_MPBoot

LLM_MPBoot is a pipeline for generating ontology mappings from relational database dumps and target ontologies using LLMs.

The current workflow is script-driven:
- bootstrap the environment
- optionally download the selected RODI datasets
- generate PostgreSQL-compatible dataset copies
- generate OWL/XML ontology files where needed
- run the mapping pipeline for one dataset or for a full batch

## Requirements

- Python 3.9+
- Java 11+ for ROBOT
- `curl`
- `git` if you want the bootstrapper to download RODI datasets

## Main scripts

All commands below assume you are at the repository root:

```bash
cd mpboot_llm
```

### 1. Bootstrap

Create the virtual environment, install Python dependencies, and install ROBOT locally under `.tools/robot/`:

```bash
bash scripts/bootstrap.sh
```

To also download the selected RODI benchmark datasets into `datasets/rodi/`:

```bash
bash scripts/bootstrap.sh --download-rodi
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

### 2. Generate PostgreSQL-compatible datasets

Build the PostgreSQL-compatible dataset tree from `datasets/rodi/`:

```bash
bash scripts/create_pg_compatible_dataset.sh datasets/rodi
```

This writes to:

```text
pg_compatible/outputs/data_pg_compatible/
```

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

## Script summary

- [scripts/bootstrap.sh](scripts/bootstrap.sh)
  - bootstraps the environment
  - optionally downloads the selected RODI datasets

- [scripts/create_pg_compatible_dataset.sh](scripts/create_pg_compatible_dataset.sh)
  - builds PostgreSQL-compatible dataset copies

- [scripts/generate_owlxml_ontologies.sh](scripts/generate_owlxml_ontologies.sh)
  - generates `ontology.owl` files from `ontology.ttl` using ROBOT

- [scripts/create_mapping_single_dataset.sh](scripts/create_mapping_single_dataset.sh)
  - runs the mapping pipeline for one dataset

- [scripts/create_all_mapping.sh](scripts/create_all_mapping.sh)
  - runs the mapping pipeline for a dataset batch and archives outputs

- [scripts/evaluation.sh](scripts/evaluation.sh)
  - reserved for the future evaluation workflow

## Current notes

- The mapping phases still operate through the existing phase scripts under `src/agents/` and `src/parsers/`.
- The single-dataset and batch orchestration logic now lives under `src/runners/`.
- The evaluation runner is currently only a placeholder.
