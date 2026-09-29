"""build-site, the backend query layer, publish-site and the loader, on a small site built from the real fixtures."""
import csv, gzip, json, os
import duckdb, openpyxl
import pytest
from rate_visualizer import build_db, config, nppes
from rate_visualizer.backend import Backend, Filters, Settings, SiteDataError, ensure_site_db, open_backend
from rate_visualizer.backend import schema as S
from rate_visualizer.backend.storage import LocalStore, StorageError, store_from_settings
from rate_visualizer.sitebuild import build_site, publish
from test_build import make_run
from test_extract import FIXTURE, KELSEY
from test_nppes import fake_nppes_zip

MARK = "Blue-Cross-and-Blue-Shield-of-Texas_"
DATE = "2026-01-01"
T, SW = nppes.TAXONOMY_CODE.format, nppes.TAXONOMY_SWITCH.format


def write_cfg(tmp):
    tags = tmp / "tags.csv"
    p = tmp / "cfg.toml"
    p.write_text(f'state = "TX"\nindex_path = "{tmp / "idx.json"}"\nin_state_filename_marker = "{MARK}"\n'
                 f'codes = {json.dumps(list(S.CODES))}\ncode_type = "CPT"\ndata_dir = "{tmp / "data"}"\n'
                 f'entity_tags_path = "{tags}"\nsite_months_kept = 2\n')
    return config.load(p), tags


def geo_files(tmp):
    z = tmp / "zcta.txt"
    z.write_text("GEOID|GEOIDFQ|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG\n"
                 "77030|x|1|0|1|0|29.706|-95.402\n78701|x|1|0|1|0|30.271|-97.743\n76504|x|1|0|1|0|31.142|-97.375\n")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ZIP_CODE", "PO_NAME", "STATE", "ZIP_TYPE", "zcta", "zip_join_type"])
    ws.append(["76508", "Temple", "TX", "Post Office or large volume customer", "76504", "Spatial join to ZCTA"])
    ws.append(["76508", "Temple", "TX", "duplicate row", "76504", "Spatial join to ZCTA"])
    x = tmp / "xwalk.xlsx"
    wb.save(x)
    return str(z), str(x)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("site")
    cfg, tags = write_cfg(tmp)
    make_run(tmp, cfg, [(f"https://h/{os.path.basename(FIXTURE)}", FIXTURE),
                        (f"https://h/{os.path.basename(KELSEY)}", KELSEY)])
    build_db.build(cfg, DATE)
    db = os.path.join(cfg.data_dir, "TX", DATE, "rates.duckdb")
    with duckdb.connect(db, read_only=True) as con:
        npis = [r[0] for r in con.execute("""SELECT npi FROM rates_npi WHERE billing_class = 'professional'
            AND rate_unit = 'dollars' AND is_valid_npi AND billing_code IN ('90837', '99214')
            GROUP BY npi HAVING count(DISTINCT billing_code) = 2 ORDER BY npi LIMIT 5""").fetchall()]
        tin = con.execute("SELECT tin_value FROM rates_npi WHERE npi = ? LIMIT 1", [npis[0]]).fetchone()[0]
    people = {  # npi -> (taxonomy, practice zip)
        npis[0]: ("2084P0800X", "77030"),   # psychiatrist, direct ZIP
        npis[1]: ("1041C0700X", "78701"),   # LCSW
        npis[2]: ("1041C0700X", "76508"),   # LCSW, ZIP only via crosswalk
        npis[3]: ("225100000X", "77030"),   # physical therapist -> ghost candidate
        npis[4]: ("207Q00000X", "99999"),   # family medicine -> non_bh_clinician, unmapped ZIP
    }
    recs = [{"NPI": n, "Entity Type Code": "1", "Provider First Name": f"P{i}", "Provider Last Name (Legal Name)": "TEST",
             "Provider Business Practice Location Address City Name": "HOUSTON" if z == "77030" else "AUSTIN",
             "Provider Business Practice Location Address State Name": "TX",
             "Provider Business Practice Location Address Postal Code": z + "1234", T(1): tx, SW(1): "Y"}
            for i, (n, (tx, z)) in enumerate(people.items())]
    recs.append({"NPI": "1013915255", "Entity Type Code": "2", T(1): "261QM1300X", SW(1): "Y",
                 "Provider Organization Name (Legal Business Name)": "KELSEY-SEYBOLD MEDICAL GROUP, PLLC"})
    fake_nppes_zip(tmp / "n.zip", recs)
    (tmp / "nucc.csv").write_text(
        "Code,Grouping,Classification,Specialization,Definition,Notes,Display Name,Section\n"
        "2084P0800X,Physicians,Psychiatry & Neurology,Psychiatry,d,n,Psychiatry Physician,Individual\n"
        "1041C0700X,Behavioral Health,Social Worker,Clinical,d,n,Clinical Social Worker,Individual\n"
        "225100000X,Therapy,Physical Therapist,,d,n,Physical Therapist,Individual\n"
        "207Q00000X,Physicians,Family Medicine,,d,n,Family Medicine Physician,Individual\n"
        "261QM1300X,Facilities,Clinic/Center,Multi-Specialty,d,n,Multi-Specialty Clinic,Non-Individual\n")
    nppes.run(cfg, DATE, nppes_zip=str(tmp / "n.zip"), nucc_csv=str(tmp / "nucc.csv"), log=lambda m: None)
    with open(tags, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tin", "tag_type", "tag_name", "relationship", "confidence", "match_method", "evidence",
                    "n_npis_2026_08_20"])
        w.writerow([tin, "platform", "TestPlatform", "platform_entity", "confirmed", "test", "test", "1"])
        w.writerow(["76-0386391", "health_system", "TestSystem", "system", "medium", "test", "test", "1"])
    path, counts = build_site.build(cfg, DATE, geo_paths=geo_files(tmp), log=lambda m: None)
    return {"cfg": cfg, "path": path, "counts": counts, "people": people, "npis": npis, "tin": tin, "tmp": tmp,
            "rates_db": db}


@pytest.fixture(scope="module")
def b(site):
    backend = Backend(site["path"], threads=1)
    yield backend
    backend.close()


def q(site, sql, params=()):
    # same settings as the Backend fixture: DuckDB refuses a second connection with different settings
    with duckdb.connect(site["path"], read_only=True, config={"memory_limit": "1GB", "threads": 1}) as con:
        return con.execute(sql, params).fetchall()


# ---- build-site -------------------------------------------------------------------------------------------------
def test_site_tables_and_counts(site):
    c = site["counts"]
    with duckdb.connect(site["rates_db"], read_only=True) as con:
        dedup = con.execute("SELECT count(*) FROM rates_npi_dedup").fetchone()[0]
        sources = con.execute("SELECT sum(n_sources) FROM rates_npi_dedup").fetchone()[0]
    assert c["rates"] == dedup and c["rate_sources"] == sources
    assert c["capitation"] == 1 and c["entity_tags"] == 2
    ids = q(site, "SELECT min(rate_id), max(rate_id), count(DISTINCT rate_id) FROM rates")[0]
    assert ids == (1, dedup, dedup)  # unique, gap-free ids


def test_providers_types_scope_and_geo(site):
    p = site["people"]
    rows = {r[0]: r[1:] for r in q(site, "SELECT npi, provider_type_group, lat, geo_source, in_nppes FROM providers")}
    n = site["npis"]
    assert rows[n[0]][0] == "Psychiatrist" and rows[n[0]][2] == "practice_zip"
    assert rows[n[2]][0] == "LCSW" and rows[n[2]][2] == "practice_zip_crosswalk" and rows[n[2]][1] == 31.142
    assert rows[n[3]][0] == "Other" and rows[n[4]][1] is None
    flags = dict(q(site, """SELECT npi, any_value(scope_flag) FROM rates WHERE billing_code = '90837' AND npi IN (?, ?, ?, ?)
                           GROUP BY 1""", [n[0], n[1], n[3], n[4]]))
    assert flags == {n[0]: "expected", n[1]: "expected", n[3]: "ghost_candidate", n[4]: "non_bh_clinician"}
    # LCSW can't bill E/M: E/M rows for an LCSW are ghost candidates
    assert q(site, "SELECT DISTINCT scope_flag FROM rates WHERE npi = ? AND billing_code = '99214'", [n[1]]) == [("ghost_candidate",)]
    assert q(site, "SELECT DISTINCT scope_flag FROM rates WHERE npi = ? AND billing_code = '99214'", [n[0]]) == [("expected",)]
    others = q(site, "SELECT count(*) FROM rates WHERE is_valid_npi AND npi NOT IN (SELECT npi FROM providers WHERE in_nppes)")[0][0]
    assert others == q(site, "SELECT count(*) FROM rates WHERE scope_flag = 'not_in_nppes' AND is_valid_npi")[0][0]


def test_tags_denormalised_and_dq(site):
    assert q(site, "SELECT DISTINCT tag_name, tag_confidence FROM rates WHERE tin = ?", [site["tin"]]) == \
        [("TestPlatform", "confirmed")]
    stats = dict(q(site, "SELECT metric, value FROM dq_stats"))
    assert stats["rows_after_dedup"] == site["counts"]["rates"]
    assert stats["rows_before_dedup"] - stats["rows_after_dedup"] == stats["rows_removed_by_dedup"]
    assert stats["zero_rate_rows"] == q(site, "SELECT count(*) FROM rates WHERE is_zero_rate")[0][0] > 0


def test_build_site_rejects_duplicate_tags(site, tmp_path):
    cfg = site["cfg"]
    bad = tmp_path / "dup.csv"
    bad.write_text("tin,tag_type,tag_name,relationship,confidence,match_method,evidence,n_npis_2026_08_20\n"
                   "1,platform,A,x,high,x,x,1\n1,platform,B,x,high,x,x,1\n")
    with pytest.raises(build_site.SiteBuildError, match="more than once"):
        build_site.build(cfg.__class__(**{**cfg.__dict__, "entity_tags_path": str(bad)}), DATE,
                         geo_paths=geo_files(tmp_path), log=lambda m: None)
    assert os.path.exists(site["path"])  # the good site.duckdb is untouched


# ---- Filters -----------------------------------------------------------------------------------------------------
def test_filters_basics():
    f = Filters(codes=["90837", "90834"], networks="Blue Essentials")
    assert f.codes == ("90834", "90837") and f.networks == ("Blue Essentials",)
    assert hash(f) == hash(Filters.from_dict({"codes": ["90837", "90834"], "networks": ["Blue Essentials"], "junk": 1}))
    assert Filters().to_dict()["bh_providers_only"] is True
    with pytest.raises(ValueError):
        Filters(counting_unit="zip")
    with pytest.raises(ValueError):
        Filters(tag_types=("vendor",))
    sql, params = Filters(search="o'brien").where()
    assert "o'brien" not in sql and "%O'BRIEN%" in params  # user text only ever goes in as a parameter


# ---- Backend -----------------------------------------------------------------------------------------------------
def test_meta_health_options(b):
    assert b.health()["ok"] and b.meta()["snapshot_date"] == DATE
    o = b.filter_options()
    assert [c["value"] for c in o["codes"]] == sorted(S.CODES)
    assert o["networks"] == ["Blue Essentials"]
    assert {p["value"] for p in o["places_of_service"]} >= {"11", "22"}
    assert {"tag_type": "platform", "tag_name": "TestPlatform", "n_tins": 1} in o["tag_names"]
    assert o["defaults"]["counting_unit"] == "tin"


def _direct(site, f, code):
    where, params = f.where("r", market=True)
    return q(site, f"SELECT count(DISTINCT tin), count(DISTINCT npi), count(*) FROM rates r WHERE {where} AND billing_code = ?",
             params + [code])[0]


@pytest.mark.parametrize("f", [
    Filters(),
    Filters(bh_providers_only=False),
    Filters(bh_providers_only=False, exclude_zero=False),
    Filters(bh_providers_only=False, counting_unit="npi"),
    Filters(bh_providers_only=False, counting_unit="row"),
    Filters(bh_providers_only=False, pos_groups=("non_facility",)),
    Filters(bh_providers_only=False, places_of_service=("22",)),
    Filters(bh_providers_only=False, provider_types=("LCSW",)),
    Filters(bh_providers_only=False, cities=("HOUSTON",)),
    Filters(bh_providers_only=False, tag_types=("platform",)),
    Filters(bh_providers_only=False, tag_types=("none",)),
    Filters(bh_providers_only=False, networks=("Blue Essentials",), texas_files_only=False),
])
def test_code_summary_matches_direct_sql(b, site, f):
    res = b.code_summary(f)
    assert res["counting_unit"] == f.counting_unit
    for row in res["rows"]:
        assert (row["n_tins"], row["n_npis"], row["n_rows"]) == _direct(site, f, row["billing_code"])
        vals = [row[k] for k in ("min", "p10", "p25", "p50", "p75", "p90", "max")]
        assert all(a <= b_ + 1e-9 for a, b_ in zip(vals, vals[1:]))  # float rounding in quantiles
        expected_units = {"tin": row["n_tins"], "npi": row["n_npis"], "row": row["n_rows"]}[f.counting_unit]
        assert row["n_units"] == expected_units


def test_toggles_narrow(b):
    base = Filters(bh_providers_only=False, exclude_zero=False)
    total = lambda f: sum(r["n_rows"] for r in b.code_summary(f)["rows"])
    assert total(base) > total(base.replace(exclude_zero=True)) > 0
    assert total(base) > total(base.replace(bh_providers_only=True)) > 0
    assert total(base.replace(codes=("90837",))) < total(base)


def test_breakdowns(b):
    f = Filters(bh_providers_only=False)
    for by in ("provider_type", "network", "place_of_service", "tag"):
        rows = b.code_summary(f, by)["rows"]
        assert rows and all("breakdown" in r for r in rows)
    pct = b.code_summary(Filters(bh_providers_only=False, billing_classes=("institutional",)))["rows"]
    assert pct == [] or all(r["pct_percentage_rows"] is not None for r in pct)
    with pytest.raises(ValueError):
        b.code_summary(f, "zip")


def test_histogram_and_benchmarks(b, site):
    f = Filters(bh_providers_only=False)
    h = b.histogram(f, "90837", bins=10)
    assert h["n"] == sum(h["counts"]) + h["underflow"] + h["overflow"] and len(h["edges"]) == 11
    assert [m["tag_name"] for m in h["markers"]] == ["TestPlatform"]
    bm = b.benchmarks(f, codes=("90837",))["rows"]
    assert bm and all(r["tag_name"] == "TestPlatform" for r in bm)
    assert all(0 <= r["pct_below"] <= r["pct_at_or_below"] <= 100 for r in bm)
    # medium-confidence tags are hidden at the default min confidence ("high") and shown at "medium"
    assert all(r["tag_name"] != "TestSystem" for r in b.benchmarks(f)["rows"])


def test_rate_explorer_paging_sorting_summaries(b):
    f = Filters(bh_providers_only=False, codes=("90837",))
    p1 = b.rate_explorer(f, page=1, page_size=5, sort_by="negotiated_rate", sort_dir="desc")
    p2 = b.rate_explorer(f, page=2, page_size=5, sort_by="negotiated_rate", sort_dir="desc")
    rates = [r["negotiated_rate"] for r in p1["rows"] + p2["rows"]]
    assert p1["total"] == p2["total"] >= 10 and rates == sorted(rates, reverse=True)
    assert {r["rate_id"] for r in p1["rows"]}.isdisjoint(r["rate_id"] for r in p2["rows"])
    row = p1["rows"][0]
    assert {"provider_name", "tin_name", "tag_name", "networks", "service_code", "scope_flag"} <= set(row)
    s = b.rate_explorer(f, summarize_by="tin_code")
    assert sum(r["n_rows"] for r in s["rows"]) == p1["total"]
    assert b.rate_explorer(f, summarize_by="code")["rows"][0]["n_rows"] == p1["total"]
    from rate_visualizer.backend.queries import SUMMARIZE
    for by in SUMMARIZE:  # every group-by option works and covers every row (network counts once per network)
        res = b.rate_explorer(f, summarize_by=by, page_size=5000)
        assert res["keys"][-1] == "billing_code" and res["rows"]
        if by != "network_code":
            assert sum(r["n_rows"] for r in res["rows"]) == p1["total"]
    with pytest.raises(ValueError):
        b.rate_explorer(f, sort_by="negotiated_rate; DROP TABLE rates")


def test_search_and_drilldown(b, site):
    n = site["npis"][0]
    f = Filters(bh_providers_only=False)
    by_npi = b.rate_explorer(f.replace(search=n[:6]))
    assert by_npi["total"] > 0 and all(n[:6] in r["npi"] or n[:6] in r["tin"] for r in by_npi["rows"])
    assert b.rate_explorer(f.replace(search="p0 test"))["total"] == b.rate_explorer(f.replace(npis=(n,)))["total"]
    drill = b.rate_explorer(f.replace(tins=(site["tin"],)))
    assert drill["total"] > 0 and {r["tin"] for r in drill["rows"]} == {site["tin"]}


def test_percentage_rates_and_export(b, tmp_path):
    f = Filters(bh_providers_only=False, billing_classes=("institutional",))
    pr = b.percentage_rates(f)
    assert pr["total"] > 0 and all(r["rate_unit"] == "percent_of_billed_charges" for r in pr["rows"])
    assert all(r["billed_charge"] is None and "estimated_dollars" in r for r in pr["rows"])
    out = tmp_path / "x.csv"
    n = b.export_csv(Filters(bh_providers_only=False, codes=("90837",)), out)
    with open(out) as fh:
        assert len(fh.readlines()) == n + 1
    assert b.export_csv(f, tmp_path / "p.csv", percentage=True) == pr["total"]


def test_map_and_zip_providers(b, site):
    m = b.map_points(Filters(bh_providers_only=False), "90837")
    pts = {(p["lat"], p["lon"]): p for p in m["points"]}
    assert (29.706, -95.402) in pts and (31.142, -97.375) in pts
    assert m["unmapped_providers"] > 0  # NPIs not in the tiny NPPES + the 99999 ZIP
    assert all(0 <= p["market_percentile"] <= 100 for p in m["points"])
    who = b.zip_providers(Filters(bh_providers_only=False), 29.706, -95.402, "90837")
    assert {w["npi"] for w in who} <= {site["npis"][0], site["npis"][3]} and who


def test_composition_profiles_lists(b, site):
    f = Filters(bh_providers_only=False)
    comp = b.rate_type_composition(f)
    assert {r["negotiated_type"] for r in comp} >= {"negotiated"}
    prof = b.provider_profile(site["npis"][0], f)
    assert prof["provider"]["provider_type_group"] == "Psychiatrist" and prof["per_code"]
    assert prof["tins"] and prof["networks"] == ["Blue Essentials"]
    assert all(0 <= r["pct_at_or_below"] <= 100 for r in prof["per_code"])
    ent = b.billing_entity_profile(site["tin"], f)
    assert ent["entity"]["tag_name"] == "TestPlatform" and ent["n_npis"] >= 1
    assert all(r["uniform"] in (True, False) for r in ent["per_code"])
    pl = b.provider_list(f, page_size=10)
    el = b.entity_list(f, page_size=10)
    assert pl["total"] > 0 and el["total"] > 0 and len(pl["rows"]) <= 10


def test_data_quality_and_sources(b, site):
    dq = b.data_quality()
    stats = {s["metric"]: s["value"] for s in dq["stats"]}
    assert b.dq_rows("zero_rate")["total"] == stats["zero_rate_rows"]
    assert b.dq_rows("scope:ghost_candidate")["total"] == stats["scope_ghost_candidate_rows"]
    fid = dq["file_rows"][0]["file_id"]
    assert b.dq_rows(f"file:{fid}")["total"] > 0
    with pytest.raises(ValueError):
        b.dq_rows("everything")
    rid = b.rate_explorer(Filters(bh_providers_only=False, codes=("90837",)), page_size=1)["rows"][0]
    src = b.source_rows(rid["rate_id"])
    assert src and src[0]["price_path"].startswith("in_network[")
    with gzip.open(FIXTURE, "rt") as fh:
        doc = json.load(fh, parse_float=str)
    s0 = src[0]
    price = doc["in_network"][s0["i"]]["negotiated_rates"][s0["j"]]["negotiated_prices"][s0["k"]]
    assert float(price["negotiated_rate"]) == rid["negotiated_rate"]
    assert str(doc["provider_references"][s0["m"]]["provider_groups"][s0["n"]]["npi"][s0["p"]]) == rid["npi"]
    assert b.capitation()[0]["covered_target_codes"] == ["99205", "99214", "99215"]


def test_suggest_and_cache(b, site):
    assert b.suggest("city", "hous")[0]["value"] == "HOUSTON"
    assert b.suggest("provider", site["npis"][0][:5])
    assert b.suggest("entity", site["tin"][:5])
    f = Filters()
    assert b.code_summary(f) is b.code_summary(f)  # second call comes from the cache


def _round(v):
    if isinstance(v, float):
        return round(v, 9)
    if isinstance(v, list):
        return [_round(x) for x in v]
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    return v


def test_core_table_routing_gives_identical_answers(b, site, monkeypatch):
    """Every method answers the same from rates_core as from the full rates table."""
    f = Filters()
    assert f.fits_core() and not f.replace(exclude_zero=False).fits_core()
    assert f.fits_core(ignore={"dollar_rates_only"}) and not f.fits_core(ignore={"bh_providers_only"})
    assert not f.replace(billing_classes=("institutional",)).fits_core()
    n = site["npis"][0]
    calls = [lambda: b.code_summary(f), lambda: b.code_summary(f, "network"), lambda: b.histogram(f, "90837"),
             lambda: b.benchmarks(f), lambda: b.rate_explorer(f, sort_by="rate_id"), lambda: b.map_points(f, "90837"),
             lambda: b.provider_profile(n, f), lambda: b.billing_entity_profile(site["tin"], f),
             lambda: b.provider_list(f), lambda: b.entity_list(f), lambda: b.rate_explorer(f, summarize_by="npi_code"),
             lambda: b.rate_type_composition(f), lambda: b.percentage_rates(f), lambda: b.rate_explorer(f, sort_by="tin_name")]
    core = [c() for c in calls]
    b._cache.clear()
    monkeypatch.setattr(Backend, "_tbl", staticmethod(lambda f, ignore=(): S.T_RATES))
    full = [c() for c in calls]
    b._cache.clear()
    assert _round(core) == _round(full)  # floats can differ in the last digit (summation order)
    core_f = Filters(dollar_rates_only=False)
    assert q(site, "SELECT count(*) FROM rates_core")[0][0] == q(site, "SELECT count(*) FROM rates r WHERE " +
                                                                  core_f.where("r")[0], core_f.where("r")[1])[0][0]


# ---- publish-site + loader --------------------------------------------------------------------------------------
def test_publish_and_load(site, tmp_path, monkeypatch):
    cfg, store = site["cfg"], LocalStore(tmp_path / "bucket")
    calls = []
    monkeypatch.setattr(publish.requests, "post", lambda url, timeout: calls.append(url) or type("R", (), {"status_code": 200})())
    settings = Settings(r2_bucket="x", render_deploy_hook_url="https://hook", site_cache_dir=str(tmp_path / "cache"),
                        site_manifest_key=f"{cfg.r2_prefix}/latest.json")
    m = publish.publish(cfg, DATE, settings, store=store, log=lambda x: None)
    assert calls == ["https://hook"] and store.size(m["key"]) == m["bytes"]
    assert json.loads(store.get_bytes(f"{cfg.r2_prefix}/latest.json"))["sha256"] == m["sha256"]
    path, man = ensure_site_db(settings, store=store, log=lambda x: None)
    assert os.path.getsize(path) == m["bytes"] and man["index_date"] == DATE
    again, _ = ensure_site_db(settings, store=store, log=lambda x: None)  # cached, no second download
    assert again == path
    be = open_backend(settings, store=store, log=lambda x: None)
    assert be.health()["ok"]
    be.close()
    # tampered upload is rejected and nothing is left behind
    store.put_bytes(m["key"], b"not a database")
    os.remove(path)
    with pytest.raises(SiteDataError, match="doesn't match"):
        ensure_site_db(settings, store=store, log=lambda x: None)
    assert not os.path.exists(path + ".part")


def test_publish_keeps_newest_months(site, tmp_path):
    cfg, store = site["cfg"], LocalStore(tmp_path / "bucket")
    for d in ("2025-10-01", "2025-11-01", "2025-12-01"):
        store.put_bytes(f"{cfg.r2_prefix}/{d}/site.duckdb", b"old")
    publish.publish(cfg, DATE, Settings(), store=store, deploy=False, log=lambda x: None)
    months = sorted({k.split("/")[-2] for k in store.list(cfg.r2_prefix + "/") if k.endswith("site.duckdb")})
    assert months == ["2025-12-01", DATE]  # site_months_kept = 2 in this config


def test_settings_and_missing_r2(tmp_path, monkeypatch):
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "RENDER_DEPLOY_HOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("# comment\nR2_ACCOUNT_ID=acct\nR2_SECRET_ACCESS_KEY='s3cret'\n")
    s = Settings.from_env(str(env))
    assert s.r2_account_id == "acct" and "s3cret" not in repr(s)
    assert s.missing_r2() == ["R2_ACCESS_KEY_ID"]
    with pytest.raises(StorageError, match="R2_ACCESS_KEY_ID"):
        store_from_settings(s)
    with pytest.raises(SiteDataError, match="R2_ACCESS_KEY_ID"):
        ensure_site_db(s, log=lambda x: None)
