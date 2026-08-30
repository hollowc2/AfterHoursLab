from afterhours_lab.db.migrate import Migration, discover_migrations


def test_discover_migrations_finds_initial_migration() -> None:
    migrations = discover_migrations()
    versions = [m.version for m in migrations]
    assert "001_initial" in versions
    assert "003_gateway_evidence" in versions
    assert "004_earnings_ohlcv_coverage" in versions
    assert "005_schwab_premarket_boundary" in versions
    assert "006_ohlcv_collection_mode" in versions
    assert "007_earnings_reaction_features" in versions
    assert "009_live_monitor" in versions


def test_discover_migrations_orders_by_filename(tmp_path) -> None:
    (tmp_path / "002_second.sql").write_text("SELECT 2;")
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    migrations = discover_migrations(tmp_path)
    assert [m.version for m in migrations] == ["001_first", "002_second"]


def test_migration_checksum_is_stable_for_same_sql() -> None:
    a = Migration(version="001_x", path=None, sql="SELECT 1;")
    b = Migration(version="001_x", path=None, sql="SELECT 1;")
    assert a.checksum == b.checksum


def test_migration_checksum_differs_for_different_sql() -> None:
    a = Migration(version="001_x", path=None, sql="SELECT 1;")
    b = Migration(version="001_x", path=None, sql="SELECT 2;")
    assert a.checksum != b.checksum
