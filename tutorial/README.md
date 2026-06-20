# Tutorial: Generate Mappings Locally with Ollama

This tutorial runs the mapping-generation pipeline on the **CMT Denormalized** dataset using a local Ollama model and a Docker-managed PostgreSQL database — no cloud API key required.

CMT is a well-known ontology alignment benchmark modelling a conference paper-review system. The denormalized variant flattens some relations, making it a cleaner starting point than the structured version.

## Prerequisites

| Tool | Purpose |
|------|---------|
| [Ollama](https://ollama.com) | Local LLM inference |
| Docker | PostgreSQL container (evaluation only) |
| Python 3.9+ | Pipeline runtime |
| Java 11+ | ROBOT ontology converter and RODI evaluation |

> **Model size matters.** A model with at least 32B parameters is strongly recommended. Smaller models (7B–8B) tend to produce malformed JSON outputs that break the pipeline. `deepseek-r1:32b` is a good default, but even at 32B output quality is not guaranteed — the model may occasionally generate invalid SQL in logical table queries or duplicate triples maps. If evaluation fails, inspect the generated `mappings_r2rml.ttl` for syntax errors. For best reliability, use a proprietary frontier model such as Claude via the `claude` provider.

---

## Step 1 — Pull the Ollama model

```bash
ollama pull deepseek-r1:32b
```

Make sure Ollama is running:

```bash
ollama serve   # keep this terminal open, or run it as a background service
```

---

## Step 2 — Configure your `.env`

From the repo root, copy the example file if you haven't already:

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
# Provider
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=deepseek-r1:32b

# PostgreSQL — only needed for the optional evaluation step
MPBOOT_DB_HOST=localhost
MPBOOT_DB_PORT=5433
MPBOOT_DB_NAME=rodi
MPBOOT_DB_USER=postgres
MPBOOT_DB_PASSWORD=postgres
MPBOOT_DB_CMD=.tools/bin/psql_docker.sh
MPBOOT_PG_CONTAINER=mpboot-postgres
MPBOOT_PG_IMAGE=postgres:11
```

---

## Step 3 — Bootstrap the toolchain

```bash
JAVA_HOME=$(/usr/libexec/java_home) bash scripts/bootstrap.sh
```

This installs the Python virtual environment, ROBOT, and the RODI evaluation tool. Only needed once.

---

## Step 4 — Run the mapping pipeline

```bash
bash scripts/create_all_mapping.sh tutorial --dataset conference_nofks
```

The pipeline stages the dataset, runs all mapping phases, and archives the result under:

```
outputs/deepseek-r1_32b/<timestamp>/conference_nofks/
  mappings_r2rml.ttl
  run_metadata.json
  run.log
```

Useful variants:

```bash
# Dry run — print the plan without calling the LLM
bash scripts/create_all_mapping.sh tutorial --dataset conference_nofks --dry-run

# Resume from a failed phase
bash scripts/create_mapping_single_dataset.sh --from phase3
```

---

## Step 5 (optional) — Evaluate with RODI

Make sure Docker is running, then:

```bash
bash scripts/evaluation.sh outputs/deepseek-r1_32b/<timestamp> --dataset conference_nofks --method all
```

Generate the summary website:

```bash
bash scripts/generate_summary_portal.sh
open outputs/summary/index.html
```

---

## Dataset layout

```
tutorial/conference_nofks/
  dump.sql          ← PostgreSQL-compatible dump (identifiers lowercased)
  ontology.owl      ← Target ontology in OWL/XML format
  queries/          ← SPARQL evaluation queries in RODI .qpair format
    Q01.qpair
    Q02.qpair
    ...
```

The dump and queries are already in PostgreSQL-compatible format (identifiers lowercased) so no conversion step is needed.
