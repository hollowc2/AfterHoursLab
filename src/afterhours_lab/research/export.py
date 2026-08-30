"""Stable exports from research datasets to CSV, Pandas, Polars, and Parquet.

The record shape is the contract: whatever ``to_record()`` produces is what lands in
every export format, so a CSV downloaded from the website and a DataFrame in a
notebook have the same columns in the same order.

Pandas, Polars, and PyArrow are optional. They are imported lazily so the CLI, the web
server, and the capture jobs never pay for a dependency they do not use.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Recordable(Protocol):
    def to_record(self) -> dict[str, Any]: ...


def to_records(rows: Iterable[Recordable]) -> list[dict[str, Any]]:
    return [row.to_record() for row in rows]


def _columns(records: Sequence[dict[str, Any]]) -> list[str]:
    """Union of keys in first-seen order, so a None-only field is never dropped."""
    columns: dict[str, None] = {}
    for record in records:
        columns.update(dict.fromkeys(record))
    return list(columns)


def _scalar(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return ";".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return value


def to_csv(rows: Iterable[Recordable]) -> str:
    """CSV text with a header row, even when the cohort is empty."""
    records = to_records(rows)
    if not records:
        return ""
    columns = _columns(records)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({key: _scalar(record.get(key)) for key in columns})
    return buffer.getvalue()


def write_csv(rows: Iterable[Recordable], path: str | Path) -> Path:
    destination = Path(path)
    destination.write_text(to_csv(rows), encoding="utf-8")
    return destination


def to_jsonl(rows: Iterable[Recordable]) -> str:
    return "".join(
        json.dumps({key: _scalar(value) for key, value in record.items()}, sort_keys=True)
        + "\n"
        for record in to_records(rows)
    )


def to_pandas(rows: Iterable[Recordable]):
    """A Pandas DataFrame. Requires the optional ``research`` extra."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise ImportError(
            "to_pandas requires pandas; install with `uv sync --extra research`"
        ) from exc
    records = to_records(rows)
    return pd.DataFrame(records, columns=_columns(records) or None)


def to_polars(rows: Iterable[Recordable]):
    """A Polars DataFrame. Requires the optional ``research`` extra."""
    try:
        import polars as pl
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise ImportError(
            "to_polars requires polars; install with `uv sync --extra research`"
        ) from exc
    records = to_records(rows)
    if not records:
        return pl.DataFrame()
    # Tuples and dataclass-derived nested values are flattened first so Polars does
    # not have to infer a struct type from a sparsely populated column.
    flattened = [{key: _scalar(value) for key, value in record.items()} for record in records]
    return pl.DataFrame(flattened)


def write_parquet(rows: Iterable[Recordable], path: str | Path) -> Path:
    """Write Parquet via PyArrow. Requires the optional ``research`` extra."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise ImportError(
            "write_parquet requires pyarrow; install with `uv sync --extra research`"
        ) from exc
    records = to_records(rows)
    flattened = [{key: _scalar(value) for key, value in record.items()} for record in records]
    table = pa.Table.from_pylist(flattened)
    destination = Path(path)
    pq.write_table(table, destination)
    return destination
