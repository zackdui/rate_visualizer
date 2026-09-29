"""Steps 2-3 tests: the real fixture checked against an independent json.load implementation, plus synthetic
edge cases (key order, inline groups, undefined/remote references, unknown keys, non-CPT codes, gzip + sha256)."""
import gzip, hashlib, json, os
from collections import Counter
import pyarrow.parquet as pq
import pytest
from rate_visualizer import config
from rate_visualizer.extract import BudgetExceeded, Limits, extract_file

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE = os.path.join(FIXTURES, "2026-08-17_Blue-Cross-and-Blue-Shield-of-Texas_Blue-Essentials-295430_"
                                 "in-network-rates.json.gz")
CODES = ["99205", "99214", "99215", "90833", "90836", "90838", "90832", "90834", "90837"]


def make_cfg(tmp_path, codes=CODES):
    p = tmp_path / "cfg.toml"
    p.write_text(f'state = "TX"\nindex_path = "x"\nin_state_filename_marker = "M"\n'
                 f'codes = {json.dumps(codes)}\ncode_type = "CPT"\n')
    return config.load(p)


def read(out, fid, name):
    return pq.read_table(os.path.join(out, fid, f"{name}.parquet")).to_pylist()


# ---------------------------------------------------------------------------------------------------------
# Independent implementation (plain json.load, numbers kept as their exact source text)
def oracle(path, codes, code_type="CPT"):
    with gzip.open(path, "rt") as fh:
        doc = json.load(fh, parse_float=str, parse_int=str)
    groups = {}
    for m, ref in enumerate(doc["provider_references"]):
        for n, g in enumerate(ref["provider_groups"]):
            for p, npi in enumerate(g["npi"]):
                groups.setdefault(ref["provider_group_id"], []).append(
                    (m, ref["provider_group_id"], n, g["tin"]["type"], g["tin"]["value"],
                     g["tin"].get("business_name"), p, npi))
    rates, used = set(), set()
    for i, item in enumerate(doc["in_network"]):
        if item["billing_code"] not in codes or item["billing_code_type"] != code_type:
            continue
        for j, nr in enumerate(item["negotiated_rates"]):
            for k, pr in enumerate(nr["negotiated_prices"]):
                for r, gid in enumerate(nr["provider_references"]):
                    used.add(gid)
                    rates.add((i, j, k, r, item["billing_code"], gid, pr["negotiated_type"], pr["negotiated_rate"],
                               pr["billing_class"], pr.get("setting"), tuple(pr.get("service_code") or []),
                               tuple(pr.get("billing_code_modifier") or []), pr["expiration_date"]))
    group_rows = {row for gid in used for row in groups[gid]}
    return doc, rates, group_rows


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fx")
    meta = extract_file(make_cfg(tmp), "fixture", "key", str(tmp / "files"), path=FIXTURE)
    return tmp / "files", meta


def test_fixture_counts(fixture_run):
    out, m = fixture_run
    assert m["sha256"] == hashlib.sha256(open(FIXTURE, "rb").read()).hexdigest()
    assert m["compressed_bytes"] == os.path.getsize(FIXTURE)
    assert (m["n_provider_references"], m["n_in_network_items"], m["n_items_matched"]) == (2036, 12763, 9)
    assert (m["n_price_rows"], m["n_rate_rows"], m["n_zero_price_rows"]) == (316, 1903, 7)
    assert m["n_groups_referenced"] == 164 and m["n_anomalies"] == 3
    assert m["last_updated_on"] == "2026-08-17" and m["version"] == "2.0.0"
    groups = read(out, "fixture", "provider_groups")
    assert len({g["npi"] for g in groups}) == 726 and len({g["tin_value"] for g in groups}) == 65
    assert m["n_provider_group_npi_rows_kept"] == len(groups)
    # BCBSTX lists NPI 0 as a placeholder in 3 provider groups; kept verbatim and flagged.
    assert sorted((a["type"], a["json_path"], a["detail"]) for a in read(out, "fixture", "anomalies")) == [
        ("npi_not_10_digits", f"provider_references[{m_}].provider_groups[0].npi[0]", "0") for m_ in (1312, 433, 705)]
    rates = read(out, "fixture", "rates")
    assert Counter((x["negotiated_type"], x["billing_class"]) for x in rates
                   if x["r"] == 0) == {("negotiated", "professional"): 307, ("percentage", "institutional"): 9}
    assert Counter((x["negotiated_type"], x["rate_unit"]) for x in rates if x["r"] == 0) == {
        ("negotiated", "dollars"): 307, ("percentage", "percent_of_billed_charges"): 9}
    assert m["peak_rss_mb"] > 0


def test_fixture_matches_independent_implementation(fixture_run):
    out, _ = fixture_run
    _, want_rates, want_groups = oracle(FIXTURE, set(CODES))
    got_rates = {(x["i"], x["j"], x["k"], x["r"], x["billing_code"], x["provider_group_id"], x["negotiated_type"],
                  x["negotiated_rate_raw"], x["billing_class"], x["setting"], tuple(x["service_code"] or []),
                  tuple(x["billing_code_modifier"] or []), x["expiration_date"])
                 for x in read(out, "fixture", "rates")}
    got_groups = {(g["m"], g["provider_group_id"], g["n"], g["tin_type"], g["tin_value"], g["tin_business_name"],
                   g["p"], g["npi"]) for g in read(out, "fixture", "provider_groups")}
    assert got_rates == want_rates
    assert got_groups == want_groups


def test_fixture_rows_trace_back_to_source(fixture_run):
    """Every rate row's (i, j, k, r) path points at the same values in the raw JSON."""
    out, _ = fixture_run
    doc, _, _ = oracle(FIXTURE, set(CODES))
    for x in read(out, "fixture", "rates")[::50]:
        item = doc["in_network"][x["i"]]
        nr = item["negotiated_rates"][x["j"]]
        assert item["billing_code"] == x["billing_code"]
        assert nr["negotiated_prices"][x["k"]]["negotiated_rate"] == x["negotiated_rate_raw"]
        assert nr["provider_references"][x["r"]] == x["provider_group_id"]
    for g in read(out, "fixture", "provider_groups")[::50]:
        ref = doc["provider_references"][g["m"]]
        assert ref["provider_group_id"] == g["provider_group_id"]
        assert ref["provider_groups"][g["n"]]["npi"][g["p"]] == g["npi"]


# ---------------------------------------------------------------------------------------------------------
SYNTHETIC = {
    "in_network": [  # deliberately before provider_references
        {"negotiated_rates": [  # deliberately before billing_code
            {"provider_references": [1, 99],
             "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 100.50,
                                    "billing_class": "professional", "expiration_date": "2999-12-31",
                                    "service_code": ["11", "02"], "billing_code_modifier": ["95"],
                                    "extra_price_key": "x"}]},
            {"provider_groups": [{"npi": [5555555555], "tin": {"type": "npi", "value": "5555555555"}}],
             "negotiated_prices": [{"negotiated_type": "percentage", "negotiated_rate": 0,
                                    "billing_class": "institutional", "expiration_date": "2999-12-31"}]}],
         "billing_code": "90837", "billing_code_type": "CPT", "negotiation_arrangement": "ffs", "name": "PSYTX"},
        {"billing_code": "90837", "billing_code_type": "HCPCS", "negotiated_rates": [
            {"provider_references": [2], "negotiated_prices": [{"negotiated_type": "negotiated",
                                                                "negotiated_rate": 1}]}]},
        {"billing_code": "99999", "billing_code_type": "CPT", "negotiated_rates": []},
    ],
    "version": "2.0.0",
    "provider_references": [
        {"provider_group_id": 1, "network_name": ["N1"],
         "provider_groups": [{"npi": [1111111111, 2222222222],
                              "tin": {"type": "ein", "value": "12-3456789", "business_name": "Biz"}}]},
        {"provider_group_id": 2, "network_name": ["N1"],
         "provider_groups": [{"npi": [3333333333], "tin": {"type": "ein", "value": "98-7654321"}}]},
        {"provider_group_id": 3, "location": "https://example/remote.json"},
    ],
    "last_updated_on": "2026-01-01", "reporting_entity_name": "E", "surprise_top": 1,
}


@pytest.fixture()
def synthetic_run(tmp_path):
    src = tmp_path / "syn.json.gz"
    with gzip.open(src, "wt") as fh:
        json.dump(SYNTHETIC, fh)
    meta = extract_file(make_cfg(tmp_path, ["90837"]), "syn", "local:syn", str(tmp_path / "files"), path=str(src))
    return tmp_path / "files", meta, src


def test_synthetic_rows(synthetic_run):
    out, m, src = synthetic_run
    assert m["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert m["key_order"] == ["in_network", "version", "provider_references", "last_updated_on",
                              "reporting_entity_name", "surprise_top"]
    assert (m["n_in_network_items"], m["n_items_matched"], m["n_price_rows"], m["n_zero_price_rows"]) == (3, 1, 2, 1)
    rates = read(out, "syn", "rates")
    assert sorted((x["j"], x["r"], x["provider_group_id"]) for x in rates) == \
        [(0, 0, "1"), (0, 1, "99"), (1, None, "inline:0:1:0")]
    first = next(x for x in rates if x["provider_group_id"] == "1")
    assert first["negotiated_rate_raw"] == "100.5" and first["negotiated_rate"] == 100.5
    assert first["service_code"] == ["11", "02"] and first["billing_code_modifier"] == ["95"]
    assert first["name"] == "PSYTX" and first["is_zero_rate"] is False and first["rate_unit"] == "dollars"
    inline = next(x for x in rates if x["provider_group_id"].startswith("inline"))
    assert inline["rate_unit"] == "percent_of_billed_charges" and inline["is_zero_rate"] is True
    groups = read(out, "syn", "provider_groups")
    assert sorted((g["provider_group_id"], g["npi"]) for g in groups) == \
        [("1", "1111111111"), ("1", "2222222222"), ("inline:0:1:0", "5555555555")]  # group 2 not referenced
    g1 = next(g for g in groups if g["npi"] == "1111111111")
    assert (g1["network_name"], g1["tin_business_name"], g1["m"], g1["n"], g1["p"]) == (["N1"], "Biz", 0, 0, 0)


def test_synthetic_anomalies(synthetic_run):
    out, m, _ = synthetic_run
    got = sorted((a["type"], a["key"]) for a in read(out, "syn", "anomalies"))
    assert got == [("remote_provider_reference", "location"), ("undefined_provider_group", "99"),
                   ("unknown_key", "extra_price_key"), ("unknown_key", "surprise_top")]
    assert m["n_anomalies"] == 4


def _write_gz(path, doc):
    with gzip.open(path, "wt") as fh:
        json.dump(doc, fh)


def test_unknown_negotiated_type_is_flagged(tmp_path):
    doc = json.loads(json.dumps(SYNTHETIC))
    doc["in_network"][0]["negotiated_rates"][0]["negotiated_prices"][0]["negotiated_type"] = "bonus"
    _write_gz(tmp_path / "u.json.gz", doc)
    extract_file(make_cfg(tmp_path, ["90837"]), "u", "k", str(tmp_path / "files"), path=str(tmp_path / "u.json.gz"))
    rates = read(tmp_path / "files", "u", "rates")
    assert {x["rate_unit"] for x in rates if x["negotiated_type"] == "bonus"} == {"unknown"}
    assert ("unknown_negotiated_type", "negotiated_type") in {
        (a["type"], a["key"]) for a in read(tmp_path / "files", "u", "anomalies")}


@pytest.mark.parametrize("limits", [
    Limits(max_rss_bytes=1),                                   # memory limit far below any real process
    "storage",                                                 # storage limit of 1 byte (built below)
])
def test_limits_stop_file_and_leave_no_partial_output(tmp_path, limits):
    if limits == "storage":
        limits = Limits(storage_root=str(tmp_path / "files"), max_storage_bytes=1)
    out = tmp_path / "files"
    out.mkdir()
    (out / "existing.parquet").write_bytes(b"x" * 10)  # so the folder is already over a 1-byte limit
    with pytest.raises(BudgetExceeded):
        extract_file(make_cfg(tmp_path), "fx", "k", str(out), path=FIXTURE, limits=limits)
    assert sorted(os.listdir(out)) == ["existing.parquet"]  # no fx/ and no fx.tmp/


KELSEY = os.path.join(FIXTURES, "2026-08-13_Blue-Cross-and-Blue-Shield-of-Texas_Blue-Essentials_TX-Kelsey-Cap-"
                                "Table-10_in-network-rates.json.gz")


def test_real_capitation_file(tmp_path):
    m = extract_file(make_cfg(tmp_path), "kel", "k", str(tmp_path / "files"), path=KELSEY)
    out = tmp_path / "files"
    assert (m["n_in_network_items"], m["n_items_matched"], m["n_rate_rows"]) == (1, 0, 0)
    assert (m["n_capitation_items"], m["n_capitation_items_with_target_codes"], m["n_capitation_rows"],
            m["n_capitation_covered_services"], m["n_anomalies"]) == (1, 1, 1, 12428, 0)
    [cap] = read(out, "kel", "capitation")
    assert (cap["billing_code"], cap["billing_code_type"], cap["negotiation_arrangement"]) == ("CAP", "LOCAL", "capitation")
    assert (cap["negotiated_rate_raw"], cap["negotiated_rate"], cap["rate_unit"]) == ("125.00", 125.0, "dollars")
    assert cap["covered_target_codes"] == ["99205", "99214", "99215"] and cap["n_covered_services"] == 12428
    assert (cap["provider_group_id"], cap["service_code"], cap["expiration_date"]) == ("4001000061035156", ["11"], "2026-12-31")
    [g] = read(out, "kel", "provider_groups")
    assert (g["npi"], g["tin_value"], g["tin_business_name"]) == ("1013915255", "76-0386391", "KELSEY SEYBOLD MED GROUP PA")
    cov = read(out, "kel", "capitation_covered_services")
    assert len(cov) == 12428 and sorted(c["billing_code"] for c in cov if c["is_target_code"]) == ["99205", "99214", "99215"]
    with gzip.open(KELSEY, "rt") as fh:  # independent check against the raw JSON
        doc = json.load(fh, parse_float=str)
    src = doc["in_network"][0]["covered_services"]
    assert [(c["c"], c["billing_code"], c["billing_code_type"]) for c in cov] == \
        [(n, s["billing_code"], s["billing_code_type"]) for n, s in enumerate(src)]


def test_capitation_item_never_goes_to_rates(tmp_path):
    """A capitation item whose own billing_code is one of the configured codes still goes to capitation only."""
    doc = json.loads(json.dumps(SYNTHETIC))
    doc["in_network"].append({
        "billing_code": "90837", "billing_code_type": "CPT", "negotiation_arrangement": "capitation",
        "negotiated_rates": [{"provider_references": [2], "negotiated_prices": [
            {"negotiated_type": "negotiated", "negotiated_rate": 12.5, "billing_class": "professional",
             "expiration_date": "2999-12-31"}]}],
        "covered_services": [{"billing_code_type": "CPT", "billing_code": "90837"},
                             {"billing_code_type": "HCPCS", "billing_code": "90834"},  # right number, wrong type
                             {"billing_code_type": "CPT", "billing_code": "11111"}]})
    _write_gz(tmp_path / "c.json.gz", doc)
    m = extract_file(make_cfg(tmp_path, ["90837", "90834"]), "c", "k", str(tmp_path / "files"),
                     path=str(tmp_path / "c.json.gz"))
    out = tmp_path / "files"
    assert all(x["negotiation_arrangement"] != "capitation" for x in read(out, "c", "rates"))
    [cap] = read(out, "c", "capitation")
    assert cap["covered_target_codes"] == ["90837"] and cap["n_covered_services"] == 3
    assert m["n_capitation_rows"] == 1
    assert {g["provider_group_id"] for g in read(out, "c", "provider_groups")} >= {"2"}  # capitation payee kept


def test_rerun_replaces_output(synthetic_run, tmp_path):
    out, _, src = synthetic_run
    extract_file(make_cfg(tmp_path, ["90837"]), "syn", "local:syn", str(out), path=str(src))
    assert sorted(os.listdir(out)) == ["syn"]
    assert os.path.exists(os.path.join(out, "syn", "_SUCCESS"))
