from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_phase5_notebook_is_read_only_clear_and_contains_no_sql() -> None:
    notebook = json.loads((ROOT / "notebooks/04_out_of_sample_validation.ipynb").read_text())
    code = "".join(
        source
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
        for source in cell["source"]
    ).upper()
    assert not any(keyword in code for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "))
    assert all(not cell.get("outputs") for cell in notebook["cells"])
    assert all(
        cell.get("execution_count") is None
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    )


def test_phase5_persistence_has_no_gateway_or_trading_authority() -> None:
    sources = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "src/afterhours_lab/persist_outcomes.py",
            "src/afterhours_lab/studies.py",
            "src/afterhours_lab/study_cli.py",
        )
    ).lower()
    assert "from afterhours_lab.gateway" not in sources
    assert "import afterhours_lab.gateway" not in sources
    for forbidden in ("place_order", "cancel_order", "account_balance", "position_manager"):
        assert forbidden not in sources
