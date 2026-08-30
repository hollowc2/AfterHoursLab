"""Shared research-data layer.

    Database
        v
    Typed research datasets
        |-- CLI
        |-- Jupyter
        `-- Website / API

Every interface reads through this package so none of them invents its own SQL, and
no calculation is reimplemented outside ``afterhours_lab.reactions``.
"""

from afterhours_lab.research.datasets import (
    PHASES,
    STUDY_PHASES,
    STUDY_WINDOW_MINUTES,
    CohortResult,
    CoveragePhase,
    EventDetail,
    EventSummary,
    MonitorHealth,
    OperationsSnapshot,
    PathBar,
    QualityIssue,
    ResearchNote,
    TodayCandidate,
    add_note,
    fetch_class_distribution,
    fetch_cohort,
    fetch_coverage,
    fetch_event_bars,
    fetch_event_detail,
    fetch_event_summary,
    fetch_monitor_candidates,
    fetch_monitor_health,
    fetch_monthly_counts,
    fetch_notes,
    fetch_operations_snapshot,
    fetch_quality_issues,
    fetch_today,
)
from afterhours_lab.research.export import (
    to_csv,
    to_jsonl,
    to_pandas,
    to_polars,
    to_records,
    write_csv,
    write_parquet,
)
from afterhours_lab.research.filters import (
    ALGORITHM_NAME,
    ANALYSIS_STATUSES,
    COLLECTION_MODES,
    FEATURE_VERSION,
    ORDER_BY,
    REACTION_CLASSES,
    REACTION_DIRECTIONS,
    EventFilter,
    FeatureVersions,
    normalize_symbol,
)

__all__ = [
    "ALGORITHM_NAME",
    "ANALYSIS_STATUSES",
    "COLLECTION_MODES",
    "FEATURE_VERSION",
    "ORDER_BY",
    "PHASES",
    "REACTION_CLASSES",
    "REACTION_DIRECTIONS",
    "STUDY_PHASES",
    "STUDY_WINDOW_MINUTES",
    "CohortResult",
    "CoveragePhase",
    "EventDetail",
    "EventFilter",
    "EventSummary",
    "FeatureVersions",
    "OperationsSnapshot",
    "MonitorHealth",
    "PathBar",
    "QualityIssue",
    "ResearchNote",
    "TodayCandidate",
    "add_note",
    "fetch_class_distribution",
    "fetch_cohort",
    "fetch_coverage",
    "fetch_event_bars",
    "fetch_event_detail",
    "fetch_event_summary",
    "fetch_monthly_counts",
    "fetch_monitor_candidates",
    "fetch_monitor_health",
    "fetch_notes",
    "fetch_operations_snapshot",
    "fetch_quality_issues",
    "fetch_today",
    "normalize_symbol",
    "to_csv",
    "to_jsonl",
    "to_pandas",
    "to_polars",
    "to_records",
    "write_csv",
    "write_parquet",
]
