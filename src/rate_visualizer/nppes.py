"""Step 5: add NPPES names, entity type, specialty and addresses for every NPI in the database.

Automatic, no human step:
  1. Find the newest monthly full NPPES file on CMS's download page (NPPES_Data_Dissemination_<Month>_<Year>_V2.zip,
     never the weekly or deactivation files) and the newest NUCC taxonomy CSV (nucc_taxonomy_<version>.csv, highest
     version number).
  2. Download both into <data_dir>/nppes/ (checked against max_storage_gb first).
  3. Stream the npidata_pfile CSV out of the zip (never unzipped to disk) and keep only NPIs found in the database
     (provider_groups.npi, plus tin_value where tin_type = 'npi').
  4. Write <run_dir>/nppes/{nppes,taxonomy,nppes_meta,anomalies}.parquet and load them into rates.duckdb with views
     rates_npi_named, rates_npi_dedup_named, capitation_npi_named.
"""
import calendar, csv, datetime, hashlib, io, os, re, sys, zipfile
from urllib.parse import urljoin, urlparse
import duckdb, pyarrow as pa, requests
from .extract import dir_size
from .io import download, run_dir, write_parquet

NPPES_PAGE = "https://download.cms.gov/nppes/NPI_Files.html"
NUCC_PAGE = "https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57"
HEADERS = {"User-Agent": "Mozilla/5.0 (rate-visualizer)"}
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
N_TAXONOMIES = 15

# output column -> NPPES CSV header (the step fails if any header is missing)
COLUMNS = {
    "npi": "NPI",
    "entity_type_code": "Entity Type Code",
    "org_name": "Provider Organization Name (Legal Business Name)",
    "last_name": "Provider Last Name (Legal Name)",
    "first_name": "Provider First Name",
    "middle_name": "Provider Middle Name",
    "name_prefix": "Provider Name Prefix Text",
    "name_suffix": "Provider Name Suffix Text",
    "credential": "Provider Credential Text",
    "mailing_address_1": "Provider First Line Business Mailing Address",
    "mailing_address_2": "Provider Second Line Business Mailing Address",
    "mailing_city": "Provider Business Mailing Address City Name",
    "mailing_state": "Provider Business Mailing Address State Name",
    "mailing_zip": "Provider Business Mailing Address Postal Code",
    "mailing_phone": "Provider Business Mailing Address Telephone Number",
    "practice_address_1": "Provider First Line Business Practice Location Address",
    "practice_address_2": "Provider Second Line Business Practice Location Address",
    "practice_city": "Provider Business Practice Location Address City Name",
    "practice_state": "Provider Business Practice Location Address State Name",
    "practice_zip": "Provider Business Practice Location Address Postal Code",
    "practice_phone": "Provider Business Practice Location Address Telephone Number",
    "enumeration_date": "Provider Enumeration Date",
    "last_update_date": "Last Update Date",
    "deactivation_date": "NPI Deactivation Date",
    "reactivation_date": "NPI Reactivation Date",
}
TAXONOMY_CODE = "Healthcare Provider Taxonomy Code_{}"
TAXONOMY_SWITCH = "Healthcare Provider Primary Taxonomy Switch_{}"
ENTITY_TYPES = {"1": "individual", "2": "organization"}

NPPES_SCHEMA = pa.schema(
    [(c, pa.string()) for c in COLUMNS] + [
        ("entity_type", pa.string()), ("provider_name", pa.string()),
        ("taxonomy_codes", pa.list_(pa.string())), ("primary_taxonomy_code", pa.string()),
        ("primary_taxonomy_rule", pa.string()), ("specialty_grouping", pa.string()),
        ("specialty_classification", pa.string()), ("specialty_specialization", pa.string()),
        ("specialty_display_name", pa.string())])
TAXONOMY_SCHEMA = pa.schema([(c, pa.string()) for c in
                             ("code", "grouping", "classification", "specialization", "display_name", "section")])
META_SCHEMA = pa.schema([
    ("nppes_url", pa.string()), ("nppes_file", pa.string()), ("nppes_sha256", pa.string()), ("nppes_bytes", pa.int64()),
    ("csv_member", pa.string()), ("n_csv_rows", pa.int64()),
    ("nucc_url", pa.string()), ("nucc_file", pa.string()), ("nucc_version", pa.int64()), ("nucc_sha256", pa.string()),
    ("n_npis_requested", pa.int64()), ("n_found", pa.int64()), ("n_not_found", pa.int64()),
    ("n_deactivated", pa.int64()), ("n_without_specialty", pa.int64()), ("started_at", pa.string()),
    ("finished_at", pa.string())])
ANOMALY_SCHEMA = pa.schema([("step", pa.string()), ("npi", pa.string()), ("type", pa.string()), ("detail", pa.string())])


# ---- 1. find the newest files -------------------------------------------------------------------------------
def pick_nppes(html, page_url=NPPES_PAGE):
    """Newest monthly full file by (year, month) parsed from its name. Weekly/deactivation files never match."""
    found = {}
    for name, month, year in re.findall(r"(NPPES_Data_Dissemination_([A-Za-z]+)_(\d{4})_V2\.zip)", html):
        if month.lower() in MONTHS:
            found[(int(year), MONTHS[month.lower()])] = name
    if not found:
        raise SystemExit(f"no NPPES_Data_Dissemination_<Month>_<Year>_V2.zip link found on {page_url}")
    name = found[max(found)]
    return urljoin(page_url, name), name


def pick_nucc(html, page_url=NUCC_PAGE):
    """Newest NUCC taxonomy CSV by numeric version (261 > 91)."""
    found = {int(v): href for href, v in re.findall(r'href="([^"]*nucc_taxonomy_(\d+)\.csv)"', html, re.I)}
    if not found:
        raise SystemExit(f"no nucc_taxonomy_<version>.csv link found on {page_url}")
    version = max(found)
    return urljoin(page_url, found[version]), version


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.text


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- 2. taxonomy -------------------------------------------------------------------------------------------------
def read_taxonomy(path):
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = list(csv.DictReader(fh))
    need = {"Code", "Grouping", "Classification", "Specialization"}
    if not rows or not need <= set(rows[0]):
        raise SystemExit(f"{path}: NUCC CSV lacks columns {sorted(need)}")
    return [{"code": r["Code"].strip(), "grouping": r.get("Grouping"), "classification": r.get("Classification"),
             "specialization": r.get("Specialization") or None, "display_name": r.get("Display Name"),
             "section": r.get("Section")} for r in rows if r.get("Code", "").strip()]


# ---- 3. NPPES rows -----------------------------------------------------------------------------------------------
def primary_taxonomy(codes, switches):
    """Deterministic rule: the one code with switch 'Y'; else the only code listed; else None."""
    listed = [c for c in codes if c]
    primary = [c for c, s in zip(codes, switches) if c and s == "Y"]
    if len(primary) == 1:
        return primary[0], "switch_Y"
    if not primary and len(listed) == 1:
        return listed[0], "only_code"
    return None, "ambiguous" if listed else "none"


def provider_name(r):
    if r["entity_type_code"] == "2":
        return r["org_name"] or None
    parts = [r["first_name"], r["middle_name"], r["last_name"], r["name_suffix"]]
    name = " ".join(p for p in parts if p)
    return name or None


def csv_member(zf):
    members = [n for n in zf.namelist() if re.fullmatch(r"npidata_pfile_\d{8}-\d{8}\.csv", os.path.basename(n))]
    if len(members) != 1:
        raise SystemExit(f"expected exactly one npidata_pfile_<dates>.csv in the zip, found {members}")
    return members[0]


def read_nppes(zip_path, npis, taxonomy, log=lambda msg: None):
    """Stream the NPPES CSV out of the zip. Returns (rows, anomalies, member, n_csv_rows)."""
    tax = {t["code"]: t for t in taxonomy}
    rows, anomalies = [], []
    with zipfile.ZipFile(zip_path) as zf:
        member = csv_member(zf)
        with zf.open(member) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""))
            header = next(reader)
            pos = {h: i for i, h in enumerate(header)}
            need = list(COLUMNS.values()) + [f.format(k) for k in range(1, N_TAXONOMIES + 1)
                                             for f in (TAXONOMY_CODE, TAXONOMY_SWITCH)]
            missing = [h for h in need if h not in pos]
            if missing:
                raise SystemExit(f"NPPES header is missing {len(missing)} expected column(s): {missing[:5]}")
            npi_i = pos["NPI"]
            n = 0
            for line in reader:
                n += 1
                if n % 1_000_000 == 0:
                    log(f"  read {n:,} NPPES rows, matched {len(rows):,}")
                if len(line) <= npi_i or line[npi_i] not in npis:
                    continue
                if len(line) != len(header):
                    anomalies.append({"step": "nppes", "npi": line[npi_i], "type": "wrong_field_count",
                                      "detail": f"{len(line)} fields, header has {len(header)}"})
                    continue
                r = {c: (line[pos[h]].strip() or None) for c, h in COLUMNS.items()}
                codes = [line[pos[TAXONOMY_CODE.format(k)]].strip() for k in range(1, N_TAXONOMIES + 1)]
                switches = [line[pos[TAXONOMY_SWITCH.format(k)]].strip() for k in range(1, N_TAXONOMIES + 1)]
                code, rule = primary_taxonomy(codes, switches)
                if code is None and rule == "ambiguous":
                    anomalies.append({"step": "nppes", "npi": r["npi"], "type": "ambiguous_primary_taxonomy",
                                      "detail": f"codes={[c for c in codes if c]} switches={switches}"})
                t = tax.get(code) if code else None
                if code and t is None:
                    anomalies.append({"step": "nppes", "npi": r["npi"], "type": "taxonomy_code_not_in_nucc",
                                      "detail": code})
                r.update({"entity_type": ENTITY_TYPES.get(r["entity_type_code"] or ""),
                          "provider_name": provider_name(r),
                          "taxonomy_codes": [c for c in codes if c], "primary_taxonomy_code": code,
                          "primary_taxonomy_rule": rule,
                          "specialty_grouping": t and t["grouping"],
                          "specialty_classification": t and t["classification"],
                          "specialty_specialization": t and t["specialization"],
                          "specialty_display_name": t and t["display_name"]})
                rows.append(r)
    return rows, anomalies, member, n


# ---- 4. database -------------------------------------------------------------------------------------------------
VIEWS = {
    "rates_npi_named": "rates_npi",
    "rates_npi_dedup_named": "rates_npi_dedup",
    "capitation_npi_named": "capitation_npi",
}


def load_into_db(con, nppes_dir):
    """(Re)create nppes tables and the *_named views in an open DuckDB connection. No-op if step 5 never ran."""
    if not os.path.exists(os.path.join(nppes_dir, "nppes.parquet")):
        return False
    for name, table in (("nppes", "nppes"), ("taxonomy", "taxonomy"), ("nppes_meta", "nppes_meta"),
                        ("anomalies", "nppes_anomalies")):
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet('{nppes_dir}/{name}.parquet')")
    person = ("n.entity_type_code, n.entity_type, n.provider_name, n.credential, n.primary_taxonomy_code, "
              "n.specialty_grouping, n.specialty_classification, n.specialty_specialization, "
              "n.specialty_display_name, n.practice_address_1, n.practice_address_2, n.practice_city, "
              "n.practice_state, n.practice_zip, n.practice_phone, n.mailing_address_1, n.mailing_address_2, "
              "n.mailing_city, n.mailing_state, n.mailing_zip, n.mailing_phone, n.deactivation_date, "
              "n.npi IS NOT NULL AS in_nppes")
    # The TIN join is a plain equality with tin_type checked in SELECT: putting "t.tin_type = 'npi'" in the ON clause
    # stopped DuckDB from using a hash join (a 0.3 s query took over 10 minutes).
    for view, table in VIEWS.items():
        con.execute(f"""CREATE OR REPLACE VIEW {view} AS
            SELECT t.*, {person},
                   CASE WHEN t.tin_type = 'npi' THEN tn.provider_name END AS tin_npi_provider_name
            FROM {table} t
            LEFT JOIN nppes n ON n.npi = t.npi
            LEFT JOIN nppes tn ON tn.npi = t.tin_value""")
    return True


def requested_npis(db_path):
    with duckdb.connect(db_path, read_only=True) as con:
        rows = con.execute("""
            SELECT npi FROM provider_groups WHERE regexp_full_match(npi, '[0-9]{10}')
            UNION SELECT tin_value FROM provider_groups
                  WHERE tin_type = 'npi' AND regexp_full_match(tin_value, '[0-9]{10}')""").fetchall()
    return {r[0] for r in rows}


def run(cfg, index_date, nppes_zip=None, nucc_csv=None, log=print):
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    base = run_dir(cfg, index_date)
    db_path = os.path.join(base, "rates.duckdb")
    if not os.path.exists(db_path):
        raise SystemExit(f"{db_path} not found - run `rate-visualizer build-db` first")
    dl_dir = os.path.join(cfg.data_dir, "nppes")

    if nppes_zip:
        nppes_url, nppes_path = None, nppes_zip
    else:
        nppes_url, name = pick_nppes(fetch(NPPES_PAGE))
        nppes_path = os.path.join(dl_dir, name)
        if not os.path.exists(nppes_path):
            size = int(requests.head(nppes_url, headers=HEADERS, timeout=60).headers.get("Content-Length", 0))
            if dir_size(cfg.data_dir) + size > cfg.max_storage_gb * 2**30:
                raise SystemExit(f"downloading {name} ({size / 2**30:.2f} GB) would exceed max_storage_gb")
            log(f"downloading {nppes_url}")
        nppes_path = download(nppes_url, dl_dir)
    if nucc_csv:
        nucc_url, nucc_path, nucc_version = None, nucc_csv, None
    else:
        nucc_url, nucc_version = pick_nucc(fetch(NUCC_PAGE))
        nucc_path = download(nucc_url, dl_dir)
    log(f"NPPES: {os.path.basename(nppes_path)}   NUCC: {os.path.basename(nucc_path)}")

    taxonomy = read_taxonomy(nucc_path)
    npis = requested_npis(db_path)
    log(f"looking up {len(npis):,} NPIs")
    rows, anomalies, member, n_csv = read_nppes(nppes_path, npis, taxonomy, log)
    found = {r["npi"] for r in rows}
    for npi in sorted(npis - found):
        anomalies.append({"step": "nppes", "npi": npi, "type": "npi_not_in_nppes", "detail": ""})
    meta = {"nppes_url": nppes_url, "nppes_file": os.path.basename(nppes_path), "nppes_sha256": sha256_of(nppes_path),
            "nppes_bytes": os.path.getsize(nppes_path), "csv_member": member, "n_csv_rows": n_csv,
            "nucc_url": nucc_url, "nucc_file": os.path.basename(nucc_path), "nucc_version": nucc_version,
            "nucc_sha256": sha256_of(nucc_path), "n_npis_requested": len(npis), "n_found": len(found),
            "n_not_found": len(npis - found), "n_deactivated": sum(bool(r["deactivation_date"]) for r in rows),
            "n_without_specialty": sum(r["primary_taxonomy_code"] is None for r in rows), "started_at": started,
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}

    out = os.path.join(base, "nppes")
    write_parquet(rows, NPPES_SCHEMA, os.path.join(out, "nppes.parquet"))
    write_parquet(taxonomy, TAXONOMY_SCHEMA, os.path.join(out, "taxonomy.parquet"))
    write_parquet([meta], META_SCHEMA, os.path.join(out, "nppes_meta.parquet"))
    write_parquet(anomalies, ANOMALY_SCHEMA, os.path.join(out, "anomalies.parquet"))
    with duckdb.connect(db_path) as con:
        load_into_db(con, out)
        con.execute("CHECKPOINT")
    return meta, anomalies
