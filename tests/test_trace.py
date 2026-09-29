"""trace tests: path parsing, raw-path lookup with SHA-256 check, and row verification on the fixture database."""
import gzip, io, json, os
import pytest
from rate_visualizer import build_db, trace
from rate_visualizer.jsonstream import iter_top_level
from test_build import make_cfg, make_run
from test_extract import FIXTURE


def test_parse_path():
    assert trace.parse_path("in_network[5].negotiated_rates[0].negotiated_prices[1]") == [
        ("in_network", 5), ("negotiated_rates", 0), ("negotiated_prices", 1)]
    assert trace.parse_path("in_network[2].billing_code") == [("in_network", 2), ("billing_code", None)]
    for bad in ("in_network", "in_network[x]", "a[1]..b"):
        with pytest.raises(ValueError):
            trace.parse_path(bad)


def test_keep_skips_unwanted_items():
    doc = b'{"a": [{"x": [1, {"deep": [2]}]}, {"x": 2}, {"x": 3}], "b": 1}'
    got = [(k, i, v) for kind, k, i, v in iter_top_level(io.BytesIO(doc), {"a"}, keep=lambda k, i: i == 1)
           if kind == "item"]
    assert got == [("a", 1, {"x": 2})]


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tr")
    cfg = make_cfg(tmp)
    make_run(tmp, cfg, [(f"https://h/{os.path.basename(FIXTURE)}", FIXTURE)])
    build_db.build(cfg, "2026-01-01")
    return cfg


def test_trace_paths_matches_raw_json_and_sha(fixture_db):
    fid = os.listdir(os.path.join(fixture_db.data_dir, "TX", "2026-01-01", "files"))[0]
    paths = ["in_network[0].billing_code", "provider_references[433].provider_groups[0].npi"]
    found, same = trace.trace_paths(fixture_db, "2026-01-01", fid, paths, local=FIXTURE, log=lambda m: None)
    with gzip.open(FIXTURE, "rt") as fh:
        doc = json.load(fh)
    assert same is True
    assert found[paths[0]] == doc["in_network"][0]["billing_code"]
    assert [int(x) for x in found[paths[1]]] == doc["provider_references"][433]["provider_groups"][0]["npi"]


def test_trace_rows_verifies_database_values(fixture_db):
    import duckdb
    db = os.path.join(fixture_db.data_dir, "TX", "2026-01-01", "rates.duckdb")
    with duckdb.connect(db, read_only=True) as con:
        npi = con.execute("SELECT npi FROM rates_npi WHERE billing_code = '90837' AND is_valid_npi LIMIT 1").fetchone()[0]
    results = trace.trace_rows(fixture_db, "2026-01-01", npi, code="90837", limit=3, local=FIXTURE, log=lambda m: None)
    assert results
    for row, checks, same in results:
        assert same and all(checks.values()), checks
        assert set(checks) == {"negotiated_rate_raw", "billing_code", "provider_group_id (rate)", "npi",
                               "provider_group_id (group)"}


def test_local_copy_must_match_a_file_and_paths_must_exist(fixture_db, tmp_path):
    other = tmp_path / "not-in-index.json.gz"
    other.write_bytes(open(FIXTURE, "rb").read())
    with pytest.raises(SystemExit, match="doesn't match any file"):
        trace.trace_rows(fixture_db, "2026-01-01", "1234567890", local=str(other), log=lambda m: None)
    fid = os.listdir(os.path.join(fixture_db.data_dir, "TX", "2026-01-01", "files"))[0]
    with pytest.raises(SystemExit, match="does not exist in this file"):
        trace.trace_paths(fixture_db, "2026-01-01", fid, ["in_network[0].negotiated_rates[999]"], local=FIXTURE,
                          log=lambda m: None)


def test_trace_rows_can_be_limited_to_one_file(fixture_db):
    import duckdb
    db = os.path.join(fixture_db.data_dir, "TX", "2026-01-01", "rates.duckdb")
    with duckdb.connect(db, read_only=True) as con:
        npi = con.execute("SELECT npi FROM rates_npi WHERE is_valid_npi LIMIT 1").fetchone()[0]
    with pytest.raises(SystemExit, match="in file nope"):
        trace.trace_rows(fixture_db, "2026-01-01", npi, file_id="nope", log=lambda m: None)


def test_trace_detects_a_changed_file(fixture_db, tmp_path):
    """A different file than the one extracted -> SHA-256 differs."""
    fid = os.listdir(os.path.join(fixture_db.data_dir, "TX", "2026-01-01", "files"))[0]
    with gzip.open(FIXTURE, "rt") as fh:
        doc = json.load(fh)
    doc["in_network"][0]["negotiated_rates"][0]["negotiated_prices"][0]["negotiated_rate"] = 1.23
    changed = tmp_path / "changed.json.gz"
    with gzip.open(changed, "wt") as fh:
        json.dump(doc, fh)
    _, same = trace.trace_paths(fixture_db, "2026-01-01", fid, ["in_network[0].billing_code"], local=str(changed),
                                log=lambda m: None)
    assert same is False
