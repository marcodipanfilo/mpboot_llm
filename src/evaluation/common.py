from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvaluationRunConfig:
    dataset_dir: Path
    dataset_name: str
    mapping_file: Path
    ontology_file: Path
    dump_file: Path
    output_dir: Path
    rodi_root: Path
    ontop_dir: Path
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_cmd: Path
    reasoning: str = "structural"
    ontop_port: int = 8089

    @property
    def evaluation_dir(self) -> Path:
        return self.output_dir

    @property
    def rodi_output_dir(self) -> Path:
        return self.evaluation_dir / "rodi"

    @property
    def ontop_output_dir(self) -> Path:
        return self.evaluation_dir / "ontop"

    @property
    def compare_output_dir(self) -> Path:
        return self.evaluation_dir / "compare"

    @property
    def shared_output_dir(self) -> Path:
        return self.evaluation_dir / "shared"

    @property
    def queries_output_dir(self) -> Path:
        return self.evaluation_dir / "queries"

    @property
    def query_qpair_dir(self) -> Path:
        return self.queries_output_dir / "qpair"

    @property
    def query_sql_dir(self) -> Path:
        return self.queries_output_dir / "sql"

    @property
    def query_sparql_dir(self) -> Path:
        return self.queries_output_dir / "sparql"

    @property
    def query_manifest_file(self) -> Path:
        return self.queries_output_dir / "queries__manifest.json"

    @property
    def query_ontop_timings_file(self) -> Path:
        return self.queries_output_dir / "queries__ontop_timings.json"

    @property
    def qpair_dir(self) -> Path:
        return self.rodi_root / "data" / self.dataset_name / "queries"

    @property
    def rodi_config_file(self) -> Path:
        return self.rodi_root / "config.prop"

    @property
    def rodi_r2rml_dir(self) -> Path:
        return self.rodi_root / "r2rml"

    @property
    def obda_file(self) -> Path:
        return self.ontop_output_dir / "mappings__r2rml.obda"

    @property
    def ontop_patch_file(self) -> Path:
        return self.ontop_output_dir / "mappings__r2rml_ontop_patch.ttl"

    @property
    def rodi_patch_file(self) -> Path:
        return self.rodi_output_dir / "mappings__r2rml_rodi_patch.ttl"

    @property
    def ontop_properties_file(self) -> Path:
        return self.ontop_output_dir / "config__ontop_db.properties"

    @property
    def eval_rodi_report_file(self) -> Path:
        return self.rodi_output_dir / "eval_rodi__report.txt"

    @property
    def eval_rodi_tabular_file(self) -> Path:
        return self.rodi_output_dir / "eval_rodi__tabular.txt"

    @property
    def eval_rodi_timings_file(self) -> Path:
        return self.rodi_output_dir / "eval_rodi__timings.json"

    @property
    def eval_ontop_metrics_file(self) -> Path:
        return self.ontop_output_dir / "eval_ontop__metrics.json"

    @property
    def eval_ontop_summary_file(self) -> Path:
        return self.ontop_output_dir / "eval_ontop__summary.txt"

    @property
    def eval_ontop_tabular_file(self) -> Path:
        return self.ontop_output_dir / "eval_ontop__tabular.txt"

    @property
    def comparison_diff_file(self) -> Path:
        return self.compare_output_dir / "eval_compare__tabular.diff"
