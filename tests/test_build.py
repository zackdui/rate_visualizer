"""Steps 3b + 4 tests: profile and build-db on the real fixture, dedup across files/networks, storage limit."""
import gzip, json, os
from collections import Counter
import duckdb
import pytest
from rate_visualizer import build_db, config, index, profile
from rate_visualizer.extract import extract_file
from test_extract import FIXTURE, KELSEY, CODES

MARK = "Blue-Cross-and-Blue-Shield-of-Texas_"


def make_cfg(tmp_path, codes=CODES, max_storage_gb=13):
    p = tmp_path / "cfg.toml"
    p.write_text(f'state = "TX"\nindex_path = "{tmp_path / "idx.json"}"\nin_state_filename_marker = "{MARK}"\n'
                 f'codes = {json.dumps(codes)}\ncode_type = "CPT"\ndata_dir = "{tmp_path / "data"}"\n'
                 f'max_storage_gb = {max_storage_gb}\n')
    return config.load(p)


def make_run(tmp_path, cfg, sources):
    """sources: list of (location, local_path). Writes an index listing them, runs step 1, extracts each file."""
    doc = {"reporting_entity_name": "E", "last_updated_on": "2026-01-01", "version": "1",
           "reporting_structure": [{"reporting_plans": [{"plan_name": "P", "plan_id": "1"}],
                                    "in_network_files": [{"description": os.path.basename(loc), "location": loc}
                                                         for loc, _ in sources]}]}
    (tmp_path / "idx.json").write_text(json.dumps(doc))
    index.run(cfg)
    files_root = os.path.join(cfg.data_dir, "TX", "2026-01-01", "files")
    for loc, path in sources:
        key = loc.split("?")[0]
        extract_file(cfg, index.file_id_for(key), key, files_root, path=path)
    return files_root


def q(db, sql):
    with duckdb.connect(db, read_only=True) as con:
        return con.execute(sql).fetchall()


def test_fixture_profile_and_build(tmp_path):
    cfg = make_cfg(tmp_path)
    files_root = make_run(tmp_path, cfg, [(f"https://h/{os.path.basename(FIXTURE)}", FIXTURE),
                                          (f"https://h/{os.path.basename(KELSEY)}", KELSEY)])
    _, res = profile.profile(files_root)
    by_type = {(t, u, c): n for t, u, c, n in res["Prices by negotiated_type x rate_unit x billing_class"]}
    assert by_type == {("negotiated", "dollars", "professional"): 307,
                       ("percentage", "percent_of_billed_charges", "institutional"): 9}
    assert dict((r[1], r[2]) for r in res["Anomalies by type"]) == {"npi_not_10_digits": 3}

    db, counts = build_db.build(cfg, "2026-01-01")
    assert counts["rates_npi"] == 9276 and counts["rates_npi_dedup"] == 9276
    assert counts["rates_without_group"] == 0 and counts["capitation_npi"] == 1
    per_code = dict(q(db, "SELECT billing_code, count(*) FROM rates_npi GROUP BY 1"))
    assert per_code == {"90832": 1286, "90833": 945, "90834": 925, "90836": 1111, "90837": 1284, "90838": 743,
                        "99205": 992, "99214": 998, "99215": 992}
    assert q(db, "SELECT count(DISTINCT npi), count(DISTINCT tin_value) FROM rates_npi") == [(726, 65)]
    assert q(db, "SELECT count(*) FROM rates_npi WHERE NOT is_valid_npi") == [(q(db, "SELECT count(*) FROM rates_npi "
                                                                                  "WHERE npi = '0'")[0][0],)]
    assert q(db, "SELECT bool_and(is_in_state_file) FROM rates_npi") == [(True,)]
    assert q(db, "SELECT covered_target_codes, npi, tin_value FROM capitation_npi") == [
        (["99205", "99214", "99215"], "1013915255", "76-0386391")]
    assert q(db, "SELECT max(n_sources) FROM rates_npi_dedup") == [(1,)]
    assert os.listdir(os.path.dirname(db)).count("rates.duckdb.tmp") == 0


def _doc(network, service_code, rate):
    return {"reporting_entity_name": "E", "last_updated_on": "2026-01-01", "version": "2.0.0",
            "provider_references": [{"provider_group_id": 7, "network_name": [network], "provider_groups": [
                {"npi": [1111111111], "tin": {"type": "ein", "value": "12-3456789", "business_name": "Biz"}}]}],
            "in_network": [{"billing_code": "90837", "billing_code_type": "CPT", "billing_code_type_version": "2026",
                            "name": "PSYTX", "description": "d", "negotiation_arrangement": "ffs",
                            "negotiated_rates": [{"provider_references": [7], "negotiated_prices": [
                                {"negotiated_type": "negotiated", "negotiated_rate": rate, "billing_class": "professional",
                                 "setting": "outpatient", "expiration_date": "2999-12-31",
                                 "service_code": service_code}]}]}]}


def test_dedup_merges_identical_rows_across_files_and_networks(tmp_path):
    cfg = make_cfg(tmp_path, ["90837"])
    srcs = []
    for name, doc in [(f"{MARK}A.json.gz", _doc("Net A", ["11", "02"], 150.25)),   # in-state file
                      ("B.json.gz", _doc("Net B", ["02", "11"], 150.25)),          # same row, other order/network
                      ("C.json.gz", _doc("Net C", ["11", "02"], 99.00))]:          # different price
        path = tmp_path / name
        with gzip.open(path, "wt") as fh:
            json.dump(doc, fh)
        srcs.append((f"https://h/{name}", str(path)))
    make_run(tmp_path, cfg, srcs)
    db, counts = build_db.build(cfg, "2026-01-01")
    assert counts["rates_npi"] == 3 and counts["rates_npi_dedup"] == 2
    [merged] = q(db, "SELECT networks, n_sources, is_in_state_any, is_in_state_all, service_code, sources "
                     "FROM rates_npi_dedup WHERE negotiated_rate_raw = '150.25'")
    networks, n_sources, any_in, all_in, service_code, sources = merged
    assert networks == ["Net A", "Net B"] and n_sources == 2 and any_in is True and all_in is False
    assert service_code == ["02", "11"]
    assert sorted(s["service_code"] for s in sources) == [["02", "11"], ["11", "02"]]  # original order kept per source
    assert q(db, "SELECT n_sources, networks FROM rates_npi_dedup WHERE negotiated_rate_raw = '99.0'") == [(1, ["Net C"])]


def test_storage_limit_leaves_no_database(tmp_path):
    cfg = make_cfg(tmp_path)
    make_run(tmp_path, cfg, [(f"https://h/{os.path.basename(FIXTURE)}", FIXTURE)])
    tiny = make_cfg(tmp_path, max_storage_gb=0.000001)
    with pytest.raises(build_db.StorageLimit):
        build_db.build(tiny, "2026-01-01")
    run = os.path.join(cfg.data_dir, "TX", "2026-01-01")
    assert not [f for f in os.listdir(run) if f.startswith("rates.duckdb")]
