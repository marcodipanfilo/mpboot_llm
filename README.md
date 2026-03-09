# LLM_MPBoot

LLM_MPBoot is a system that leverages Large Language Models to automatically generate mappings between relational databases and ontologies. Given a relational database schema and a target ontology, the system uses LLMs to produce candidate mappings (e.g., in R2RML or similar mapping languages) that describe how database tables and columns relate to ontology classes and properties. The pipeline supports bootstrapping ontology-based data access (OBDA) setups by reducing the manual effort traditionally required to create these mappings, and allows experimentation with different LLM models to compare mapping quality.

## Getting Started

### 1. Clone the Repository

```bash
git clone https://gitlab.inf.unibz.it/in2data/mpboot_llm.git
cd mpboot_llm
code .
```

### 2. Create and Activate a Virtual Environment

```bash
python3.9 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Create a `.env` File

Create a `.env` file in the project root and add the required environment variables (e.g., API keys for the LLM provider you plan to use).

### 5. Select the Python Interpreter in VS Code

Open the Command Palette (`Ctrl+Shift+P`) and choose **Python: Select Interpreter**, then point it to:

```
.venv/bin/python
```

## Configuration

The file `config/llm_config.py` controls which LLM model is used by the pipeline. Open it and update the model name or parameters to switch between different models.

## Running the Pipeline

Before running, clear any old results or output files:

```bash
python src/tests/clear_files.py --confirm
```

Then run the full pipeline:

```bash
python src/tests/pipeline_run.py
```
