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
                "response_sha256": "a" * 64,
                "data_quality_flags": [],
            }
        ]
    )
    assert f"AAPL\tearnings_regular\t389/390\tfirst\tlast\t{'a' * 64}\t-" in text
    assert "AAPL\tearnings_postmarket\tMISSING" in text
    assert "AAPL\tfollowing_premarket\tMISSING" in text
    assert "AAPL\tfollowing_regular\tMISSING" in text
