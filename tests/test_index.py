"""Step 1 tests: synthetic edge cases + the real 2026-08-20 BCBSTX index (skipped if not downloaded)."""
import io, json, os
from collections import Counter
import pytest
from rate_visualizer import config
from rate_visualizer.index import parse_index, run
from rate_visualizer.jsonstream import iter_top_level

ROOT = os.path.dirname(os.path.dirname(__file__))
REAL_INDEX = os.path.join(ROOT, "mrf_data", "2026-08-20_Blue-Cross-and-Blue-Shield-of-Texas_index.json")
MARK = "Blue-Cross-and-Blue-Shield-of-Texas_"


def make_cfg(tmp_path, index_path):
    p = tmp_path / "cfg.toml"
    p.write_text(f'state = "TX"\nindex_path = "{index_path}"\nin_state_filename_marker = "{MARK}"\n'
                 f'codes = ["90837"]\ncode_type = "CPT"\ndata_dir = "{tmp_path / "data"}"\n')
    return config.load(p)


def test_iter_top_level_any_key_order():
    doc = b'{"b_list": [{"x": 1}, 2, [3]], "a": "s", "obj": {"k": [1, 2]}, "after": 1.50}'
    got = list(iter_top_level(io.BytesIO(doc), {"b_list"}))
    assert [g[:2] for g in got if g[0] == "key"] == [("key", "b_list"), ("key", "a"), ("key", "obj"), ("key", "after")]
    assert [(g[2], g[3]) for g in got if g[0] == "item"] == [(0, {"x": 1}), (1, 2), (2, [3])]
    values = {g[1]: g[3] for g in got if g[0] == "value"}
    assert values["a"] == "s" and values["obj"] == {"k": [1, 2]} and str(values["after"]) == "1.50"


SYNTHETIC = {
    "reporting_structure": [  # deliberately before the scalar keys
        {"reporting_plans": [{"plan_name": "P1", "issuer_name": "I", "plan_id_type": "HIOS", "plan_id": "33602",
                              "plan_market_type": "group", "surprise": 1}],
         "in_network_files": [
             {"description": "TX net file", "location": f"https://h1/{MARK}Net_in-network-rates.json.gz"},
             {"description": "AL file", "location": "https://h2/510_x.json.gz?Expires=1&Signature=a"}],
         "allowed_amount_file": {"description": "aa", "location": "https://h1/aa.json.gz"}},  # object form
        {"reporting_plans": [{"plan_name": "P2", "plan_id": 12345}],
         "in_network_files": [
             {"description": "AL file other name", "location": "https://h2/510_x.json.gz?Expires=2&Signature=b"}],
         "allowed_amount_file": [{"description": "", "location": ""}]},  # list form, blank location
    ],
    "reporting_entity_name": "E", "last_updated_on": "2026-01-01", "version": "1.0.0", "extra_top": True,
}


def test_synthetic_index(tmp_path):
    p = tmp_path / "idx.json"
    p.write_text(json.dumps(SYNTHETIC))
    t = parse_index(p, make_cfg(tmp_path, p))
    m = t["index_meta"][0]
    assert m["last_updated_on"] == "2026-01-01" and m["key_order"][0] == "reporting_structure"
    assert (m["n_reporting_structures"], m["n_plan_rows"], m["n_file_references"], m["n_unique_files"]) == (2, 2, 4, 3)
    files = {f["file_name"]: f for f in t["index_files"]}
    assert files[f"{MARK}Net_in-network-rates.json.gz"]["is_in_state_file"] is True
    al = files["510_x.json.gz"]
    assert al["is_in_state_file"] is False and al["n_references"] == 2
    assert al["descriptions"] == ["AL file", "AL file other name"]
    assert al["url"].endswith("Signature=a")  # first occurrence kept
    assert files["aa.json.gz"]["file_kind"] == "allowed_amount"
    assert t["index_plans"][1]["plan_id"] == "12345" and t["index_plans"][1]["issuer_name"] is None
    assert Counter(a["type"] for a in t["anomalies"]) == {"unknown_key": 2, "blank_location": 1}
    assert {a["key"] for a in t["anomalies"] if a["type"] == "unknown_key"} == {"surprise", "extra_top"}


def test_run_writes_parquet(tmp_path):
    import pyarrow.parquet as pq
    p = tmp_path / "idx.json"
    p.write_text(json.dumps(SYNTHETIC))
    out, _ = run(make_cfg(tmp_path, p))
    assert out.endswith(os.path.join("TX", "2026-01-01", "index"))
    assert pq.read_table(os.path.join(out, "index_files.parquet")).num_rows == 3
    assert pq.read_table(os.path.join(out, "anomalies.parquet")).num_rows == 3


@pytest.mark.skipif(not os.path.exists(REAL_INDEX), reason="real index not downloaded")
def test_real_bcbstx_index(tmp_path):
    t = parse_index(REAL_INDEX, make_cfg(tmp_path, REAL_INDEX))
    m = t["index_meta"][0]
    assert m["last_updated_on"] == "2026-08-20"
    assert m["n_reporting_structures"] == 63
    assert m["n_plan_rows"] == 142006
    kinds = Counter(p["file_kind"] for p in t["plan_files"])
    assert kinds == {"in_network": 7079, "allowed_amount": 62}  # 63 refs, 1 blank location
    in_net = [f for f in t["index_files"] if f["file_kind"] == "in_network"]
    allowed = [f for f in t["index_files"] if f["file_kind"] == "allowed_amount"]
    assert len(in_net) == 528
    assert len(allowed) == 6  # 7 distinct locations incl. the blank one
    assert sum(f["is_in_state_file"] for f in in_net) == 36
    assert sum(len(f["descriptions"]) > 1 for f in in_net) == 38
    assert not any(f["is_in_state_file"] and len(f["descriptions"]) > 1 for f in in_net)
    assert Counter(a["type"] for a in t["anomalies"]) == {"blank_location": 1}
