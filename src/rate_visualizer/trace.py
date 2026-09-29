"""Trace numbers back to the raw source JSON.

  trace --file-id ID --path "in_network[5].negotiated_rates[0].negotiated_prices[0]" [--path ...]
      Re-streams the file (from its URL, or --local), prints the raw JSON at each path, and checks the file's SHA-256
      against the one recorded when it was extracted (file_meta.sha256).

  trace --npi NPI [--code CODE] [--limit N]
      Looks up rows in rates_npi, traces each row's two paths (the price and the NPI) and checks that the raw values
      equal the database values (negotiated_rate_raw, billing_code, provider_group_id, npi).

BCBSTX replaces files monthly, so tracing from the URL only works while the month's file is still online.
"""
import json, os, re
from collections import defaultdict
import duckdb, pyarrow.parquet as pq
from .io import open_source, run_dir
from .jsonstream import iter_top_level

_SEGMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")


def parse_path(path):
    """'a[1].b.c[2]' -> [('a', 1), ('b', None), ('c', 2)]"""
    parts = []
    for seg in path.split("."):
        m = _SEGMENT.fullmatch(seg)
        if not m:
            raise ValueError(f"bad path segment {seg!r} in {path!r}")
        parts.append((m.group(1), None if m.group(2) is None else int(m.group(2))))
    if not parts or parts[0][1] is None:
        raise ValueError(f"path must start with a top-level list and index, e.g. in_network[3]: {path!r}")
    return parts


def resolve(obj, parts, path=""):
    """Walk the remaining path segments inside an already-built object."""
    try:
        for key, idx in parts:
            obj = obj[key]
            if idx is not None:
                obj = obj[idx]
    except (KeyError, IndexError, TypeError):
        raise SystemExit(f"path {path!r} does not exist in this file (is it the right file?)")
    return obj


def fetch_paths(paths, url=None, local=None):
    """Stream the file once; return ({path: value}, sha256, bytes). Only the needed top-level items are built."""
    parsed = {p: parse_path(p) for p in paths}
    wanted = defaultdict(list)  # (top key, index) -> [paths]
    for p, parts in parsed.items():
        wanted[parts[0]].append(p)
    found = {}
    with open_source(url=url, path=local) as (stream, hasher):
        keys = {k for k, _ in wanted}
        for kind, key, idx, value in iter_top_level(stream, keys, keep=lambda k, i: (k, i) in wanted):
            if kind == "item":
                for p in wanted[(key, idx)]:
                    found[p] = resolve(value, parsed[p][1:], p)
        hasher.drain()
    missing = [p for p in paths if p not in found]
    if missing:
        raise SystemExit(f"path(s) not found in the file: {missing}")
    return found, hasher.sha256.hexdigest(), hasher.bytes


def _show(value, max_items=20):
    def trim(v):
        if isinstance(v, list):
            return [trim(x) for x in v[:max_items]] + ([f"... {len(v) - max_items} more"] if len(v) > max_items else [])
        if isinstance(v, dict):
            return {k: trim(x) for k, x in v.items()}
        return v
    return json.dumps(trim(value), indent=2, default=str)


def file_meta(cfg, index_date, file_id):
    p = os.path.join(run_dir(cfg, index_date), "files", file_id, "file_meta.parquet")
    if not os.path.exists(p):
        raise SystemExit(f"no finished extraction for file_id {file_id} ({p})")
    return pq.read_table(p).to_pylist()[0]


def trace_paths(cfg, index_date, file_id, paths, local=None, log=print):
    meta = file_meta(cfg, index_date, file_id)
    url = None if local else meta["url"]
    if not local and not url:
        raise SystemExit(f"{file_id} was extracted from a local file; pass --local <path>")
    log(f"streaming {file_id} ({meta['compressed_bytes'] / 1e6:,.1f} MB): {local or url}")
    found, sha, nbytes = fetch_paths(paths, url=url, local=local)
    same = sha == meta["sha256"]
    log(f"SHA-256 {sha} ({nbytes:,} bytes) {'MATCHES' if same else 'DIFFERS FROM'} the extracted file "
        f"({meta['sha256']})")
    return found, same


def local_file_id(cfg, index_date, local):
    """The file_id whose file name matches a local copy (same rule as `extract --local`)."""
    name = os.path.basename(local)
    p = os.path.join(run_dir(cfg, index_date), "index", "index_files.parquet")
    ids = [f["file_id"] for f in pq.read_table(p).to_pylist() if f["file_name"] == name]
    if not ids:
        raise SystemExit(f"{name} doesn't match any file in the index; pass --file-id explicitly")
    return ids[0]


def trace_rows(cfg, index_date, npi, code=None, limit=5, local=None, file_id=None, log=print):
    """Trace up to `limit` rates_npi rows for an NPI (optionally one code / one file).

    With `local`, rows are limited to that file (file_id, or the file matched by name). Returns a list of
    (row, checks dict, sha_matches).
    """
    if local and not file_id:
        file_id = local_file_id(cfg, index_date, local)
    db = os.path.join(run_dir(cfg, index_date), "rates.duckdb")
    where, params = ["npi = ?"], [npi]
    if code:
        where.append("billing_code = ?"); params.append(code)
    if file_id:
        where.append("file_id = ?"); params.append(file_id)
    sql = ("SELECT rn.file_id, billing_code, npi, provider_group_id, negotiated_rate_raw, i, j, k, r, m, n, p, "
           "f.compressed_bytes FROM rates_npi rn JOIN file_meta f USING (file_id) WHERE " + " AND ".join(where) +
           " ORDER BY f.compressed_bytes, rn.file_id, i, j, k, r, m, n, p LIMIT ?")  # smallest files first
    with duckdb.connect(db, read_only=True) as con:
        cur = con.execute(sql, params + [limit])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        raise SystemExit(f"no rates_npi rows for npi {npi}" + (f" and code {code}" if code else "")
                         + (f" in file {file_id}" if file_id else ""))
    by_file = defaultdict(list)
    for row in rows:
        row["price_path"] = f"in_network[{row['i']}].negotiated_rates[{row['j']}].negotiated_prices[{row['k']}]"
        row["code_path"] = f"in_network[{row['i']}].billing_code"
        row["ref_path"] = (f"in_network[{row['i']}].negotiated_rates[{row['j']}].provider_references[{row['r']}]"
                           if row["r"] is not None else None)
        row["npi_path"] = (f"provider_references[{row['m']}].provider_groups[{row['n']}].npi[{row['p']}]"
                           if row["m"] is not None else None)
        row["group_id_path"] = f"provider_references[{row['m']}].provider_group_id" if row["m"] is not None else None
        by_file[row["file_id"]].append(row)
    results = []
    for file_id, file_rows in by_file.items():
        paths = sorted({p for row in file_rows for p in (row["price_path"], row["code_path"], row["ref_path"],
                                                           row["npi_path"], row["group_id_path"]) if p})
        found, same = trace_paths(cfg, index_date, file_id, paths, local=local, log=log)
        for row in file_rows:
            checks = {"negotiated_rate_raw": str(found[row["price_path"]]["negotiated_rate"]) == row["negotiated_rate_raw"],
                      "billing_code": str(found[row["code_path"]]) == row["billing_code"]}
            if row["ref_path"]:
                checks["provider_group_id (rate)"] = str(found[row["ref_path"]]) == row["provider_group_id"]
            if row["npi_path"]:
                checks["npi"] = str(found[row["npi_path"]]) == row["npi"]
                checks["provider_group_id (group)"] = str(found[row["group_id_path"]]) == row["provider_group_id"]
            log(f"\n{file_id} {row['price_path']}\n{_show(found[row['price_path']])}")
            if row["npi_path"]:
                log(f"{row['npi_path']} = {found[row['npi_path']]}")
            log("checks: " + ", ".join(f"{k} {'OK' if v else 'MISMATCH'}" for k, v in checks.items()))
            results.append((row, checks, same))
    return results
