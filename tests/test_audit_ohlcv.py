from afterhours_lab.audit_ohlcv import render


def test_render_makes_missing_phases_explicit() -> None:
    text = render(
        [
            {
                "symbol": "AAPL",
                "phase": "earnings_regular",
                "observed_minutes": 389,
                "expected_minutes": 390,
                "observed_first": "first",
                "observed_last": "last",
                "calendar": "XNYS",
                "calendar_version": "4.13.2",
                "collection_mode": "scheduled_capture",
                "response_sha256": "a" * 64,
                "data_quality_flags": [],
            }
        ]
    )
    assert (
        f"AAPL\tearnings_regular\t389/390\tfirst\tlast\tscheduled_capture\t"
        f"XNYS@4.13.2\t{'a' * 64}\t-"
    ) in text
    assert "AAPL\tearnings_postmarket\tMISSING" in text
    assert "AAPL\tfollowing_premarket\tMISSING" in text
    assert "AAPL\tfollowing_regular\tMISSING" in text
