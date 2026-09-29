"""Step 5 tests: file discovery rules, primary-taxonomy rule, and the full step on the fixture database with a
small hand-built NPPES zip and NUCC CSV."""
import csv, io, os, zipfile
import duckdb
import pytest
from rate_visualizer import build_db, nppes
from test_build import make_cfg, make_run
from test_extract import FIXTURE, KELSEY

NPPES_HTML = """
<a href="./NPPES_Data_Dissemination_August_2026_V2.zip">Aug</a>
<a href="./NPPES_Data_Dissemination_September_2026_V2.zip">Sep</a>
<a href="./NPPES_Data_Dissemination_December_2025_V2.zip">Dec 2025</a>
<a href="./NPPES_Data_Dissemination_092126_092726_Weekly_V2.zip">weekly</a>
<a href="./NPPES_Deactivated_NPI_Report_091426_V2.zip">deactivated</a>"""
NUCC_HTML = """<a href="/images/stories/CSV/nucc_taxonomy_91.csv">x</a>
<a href="/images/stories/CSV/nucc_taxonomy_261.csv">y</a><a href="/images/stories/CSV/nucc_taxonomy_250.csv">z</a>"""


def test_pick_newest_files():
    assert nppes.pick_nppes(NPPES_HTML) == (
        "https://download.cms.gov/nppes/NPPES_Data_Dissemination_September_2026_V2.zip",
        "NPPES_Data_Dissemination_September_2026_V2.zip")
    url, version = nppes.pick_nucc(NUCC_HTML)
    assert version == 261 and url == "https://www.nucc.org/images/stories/CSV/nucc_taxonomy_261.csv"
    with pytest.raises(SystemExit):
        nppes.pick_nppes('<a href="./NPPES_Data_Dissemination_092126_092726_Weekly_V2.zip">')


@pytest.mark.parametrize("codes, switches, expected", [
    (["A", "B", ""], ["N", "Y", ""], ("B", "switch_Y")),
    (["A", "", ""], ["", "", ""], ("A", "only_code")),
    (["A", "B", ""], ["N", "N", ""], (None, "ambiguous")),
    (["A", "B", ""], ["Y", "Y", ""], (None, "ambiguous")),
    (["", "", ""], ["", "", ""], (None, "none")),
])
def test_primary_taxonomy_rule(codes, switches, expected):
    assert nppes.primary_taxonomy(codes, switches) == expected


def fake_nppes_zip(path, records):
    """records: list of dicts keyed by NPPES header names; every required header is present."""
    header = list(nppes.COLUMNS.values()) + [f.format(k) for k in range(1, 16)
                                             for f in (nppes.TAXONOMY_CODE, nppes.TAXONOMY_SWITCH)] + ["Extra"]
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(header)
    for rec in records:
        w.writerow([rec.get(h, "") for h in header])
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("npidata_pfile_20050523-20260913.csv", buf.getvalue())
        zf.writestr("npidata_pfile_20050523-20260913_fileheader.csv", ",".join(header))
        zf.writestr("pl_pfile_20050523-20260913.csv", "ignored")


def fake_nucc(path):
    path.write_text("Code,Grouping,Classification,Specialization,Definition,Notes,Display Name,Section\n"
                    "2084P0800X,Allopathic & Osteopathic Physicians,Psychiatry & Neurology,Psychiatry,d,n,"
                    "Psychiatry Physician,Individual\n"
                    "261QM1300X,Ambulatory Health Care Facilities,Clinic/Center,Multi-Specialty,d,n,"
                    "Multi-Specialty Clinic,Non-Individual\n")


def test_nppes_step_on_fixture_db(tmp_path):
    cfg = make_cfg(tmp_path)
    make_run(tmp_path, cfg, [(f"https://h/{os.path.basename(FIXTURE)}", FIXTURE),
                             (f"https://h/{os.path.basename(KELSEY)}", KELSEY)])
    build_db.build(cfg, "2026-01-01")
    db = os.path.join(cfg.data_dir, "TX", "2026-01-01", "rates.duckdb")
    with duckdb.connect(db, read_only=True) as con:
        person = con.execute("SELECT npi FROM rates_npi WHERE is_valid_npi ORDER BY npi LIMIT 1").fetchone()[0]
    T = nppes.TAXONOMY_CODE.format
    S = nppes.TAXONOMY_SWITCH.format
    fake_nppes_zip(tmp_path / "n.zip", [
        {"NPI": person, "Entity Type Code": "1", "Provider First Name": "ANA", "Provider Last Name (Legal Name)": "LEE",
         "Provider Credential Text": "MD", "Provider Business Practice Location Address City Name": "HOUSTON",
         "Provider Business Practice Location Address State Name": "TX",
         "Provider Business Mailing Address State Name": "OK",
         T(1): "2084P0800X", S(1): "N", T(2): "261QM1300X", S(2): "Y"},
        {"NPI": "1013915255", "Entity Type Code": "2",  # Kelsey-Seybold's organisation NPI (capitation payee)
         "Provider Organization Name (Legal Business Name)": "KELSEY-SEYBOLD MEDICAL GROUP, PLLC",
         T(1): "261QM1300X", S(1): ""},  # only one code, no Y -> only_code
        {"NPI": "9999999999", "Entity Type Code": "1"},  # not in our data -> ignored
    ])
    fake_nucc(tmp_path / "t.csv")
    meta, anomalies = nppes.run(cfg, "2026-01-01", nppes_zip=str(tmp_path / "n.zip"),
                                nucc_csv=str(tmp_path / "t.csv"), log=lambda m: None)
    assert (meta["n_found"], meta["n_csv_rows"]) == (2, 3)
    assert meta["n_not_found"] == meta["n_npis_requested"] - 2
    assert {a["type"] for a in anomalies} == {"npi_not_in_nppes"}

    with duckdb.connect(db, read_only=True) as con:
        row = con.execute("""SELECT provider_name, entity_type, credential, primary_taxonomy_code,
                                    specialty_classification, practice_city, practice_state, mailing_state, in_nppes
                             FROM rates_npi_named WHERE npi = ? LIMIT 1""", [person]).fetchone()
        assert row == ("ANA LEE", "individual", "MD", "261QM1300X", "Clinic/Center", "HOUSTON", "TX", "OK", True)
        cap = con.execute("SELECT provider_name, entity_type, specialty_specialization FROM capitation_npi_named").fetchone()
        assert cap == ("KELSEY-SEYBOLD MEDICAL GROUP, PLLC", "organization", "Multi-Specialty")
        rule = con.execute("SELECT primary_taxonomy_rule FROM nppes WHERE npi = '1013915255'").fetchone()[0]
        assert rule == "only_code"
        n_named = con.execute("SELECT count(*) FROM rates_npi_named").fetchone()[0]
        n_all = con.execute("SELECT count(*) FROM rates_npi").fetchone()[0]
        assert n_named == n_all  # LEFT JOIN: no rows lost or duplicated
        assert con.execute("SELECT count(*) FROM rates_npi_dedup_named WHERE NOT in_nppes").fetchone()[0] > 0

    # a rebuild keeps the NPPES tables and views
    build_db.build(cfg, "2026-01-01")
    with duckdb.connect(db, read_only=True) as con:
        assert con.execute("SELECT count(*) FROM rates_npi_named WHERE in_nppes").fetchone()[0] > 0
