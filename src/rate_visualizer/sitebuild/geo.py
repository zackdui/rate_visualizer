"""ZIP -> map point, from the Census ZCTA gazetteer plus HRSA's ZIP-to-ZCTA crosswalk (rates_tool_plan.md 8.5)."""
import io, os, zipfile
import openpyxl, requests
from ..io import download

ZCTA_TXT = "2025_Gaz_zcta_national.txt"
CROSSWALK_XLSX = "hrsa_zip_to_zcta.xlsx"


def ensure_geo_files(cfg, log=print):
    """Return (zcta_txt_path, crosswalk_xlsx_path), downloading into <data_dir>/geo/ if missing."""
    geo_dir = os.path.join(cfg.data_dir, "geo")
    os.makedirs(geo_dir, exist_ok=True)
    zcta = os.path.join(geo_dir, ZCTA_TXT)
    if not os.path.exists(zcta):
        log(f"downloading {cfg.geo_zcta_url}")
        r = requests.get(cfg.geo_zcta_url, timeout=300)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            member = next(n for n in zf.namelist() if n.endswith(".txt"))
            with open(zcta, "wb") as fh:
                fh.write(zf.read(member))
    xwalk = os.path.join(geo_dir, CROSSWALK_XLSX)
    if not os.path.exists(xwalk):
        log(f"downloading {cfg.geo_crosswalk_url}")
        tmp = download(cfg.geo_crosswalk_url, geo_dir)
        os.replace(tmp, xwalk)
    return zcta, xwalk


def load_geo(con, zcta_txt, crosswalk_xlsx):
    """Create geo_zcta(zip5, lat, lon) and geo_xwalk(zip5, zcta) in an open DuckDB connection."""
    con.execute(f"""CREATE OR REPLACE TEMP TABLE geo_zcta AS
        SELECT trim(GEOID) AS zip5, trim(INTPTLAT)::DOUBLE AS lat, trim(INTPTLONG)::DOUBLE AS lon
        FROM read_csv('{zcta_txt}', delim='|', header=true, all_varchar=true)""")
    rows = openpyxl.load_workbook(crosswalk_xlsx, read_only=True).worksheets[0].iter_rows(values_only=True)
    header = [str(h).strip().upper() for h in next(rows)]
    zi, ci = header.index("ZIP_CODE"), header.index("ZCTA")
    pairs = [(str(r[zi]).strip().zfill(5), str(r[ci]).strip().zfill(5)) for r in rows
             if r[zi] is not None and r[ci] is not None]
    con.execute("CREATE OR REPLACE TEMP TABLE geo_xwalk_raw (zip5 VARCHAR, zcta VARCHAR)")
    con.executemany("INSERT INTO geo_xwalk_raw VALUES (?, ?)", pairs)
    # a few ZIPs are listed twice: keep one row per ZIP (lowest ZCTA, deterministic)
    con.execute("CREATE OR REPLACE TEMP TABLE geo_xwalk AS SELECT zip5, min(zcta) AS zcta FROM geo_xwalk_raw GROUP BY 1")
    con.execute("""CREATE OR REPLACE TEMP TABLE geo_point AS  -- any ZIP -> point, direct or via the crosswalk
        SELECT zip5, lat, lon, 'direct' AS how FROM geo_zcta
        UNION ALL
        SELECT x.zip5, z.lat, z.lon, 'crosswalk' FROM geo_xwalk x JOIN geo_zcta z ON z.zip5 = x.zcta
        WHERE x.zip5 NOT IN (SELECT zip5 FROM geo_zcta)""")
