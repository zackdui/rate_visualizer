"""
BCBSTX Transparency in Coverage MRF helper.

Requires: pip install ijson requests

Every command downloads remote files to disk first, then stream-parses the
local copy (safer than parsing a live HTTP connection, and signed URLs expire).

  python bcbstx_mrf.py count                                  # how many in-network files
  python bcbstx_mrf.py toc --search "acme corp" --out files.json
  python bcbstx_mrf.py rates --url "<in_network URL>" --codes 99213 99214 --out rates.csv
  python bcbstx_mrf.py rates --file local.json.gz --codes 99213
"""
import argparse, csv, gzip, json, os, sys
from urllib.parse import urlparse
import ijson, requests

TOC_URL = ("https://app0004702110a5prdnc868.blob.core.windows.net/toc/"
           "2026-08-20_Blue-Cross-and-Blue-Shield-of-Texas_index.json")
DATA_DIR = "mrf_data"


def download(url, dest_dir=DATA_DIR):
    """Download url to dest_dir (skip if already there). Returns local path."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, os.path.basename(urlparse(url).path))
    if os.path.exists(path):
        return path
    tmp = path + ".part"
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("Content-Length", 0)), 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r{path}: {done / total:.0%}", end="", file=sys.stderr)
    print(file=sys.stderr)
    os.replace(tmp, path)
    return path


def open_local(path):
    """Open a local file, gunzipping if it starts with the gzip magic bytes."""
    with open(path, "rb") as fh:
        is_gz = fh.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if is_gz else open(path, "rb")


def resolve(url=None, path=None):
    return path if path else download(url)


def cmd_count(args):
    path = resolve(args.url, args.file)
    with open_local(path) as fh:
        locs = [f["location"].split("?")[0] for f in
                ijson.items(fh, "reporting_structure.item.in_network_files.item")]
    print(f"{len(locs)} references | {len(set(locs))} unique in-network files")


def cmd_toc(args):
    path = resolve(args.url, args.file)
    term, matches = args.search.lower(), []
    with open_local(path) as fh:
        for struct in ijson.items(fh, "reporting_structure.item"):
            plans = struct.get("reporting_plans", [])
            hit = [p for p in plans
                   if term in str(p.get("plan_name", "")).lower()
                   or term == str(p.get("plan_id", "")).lower()]
            if not hit:
                continue
            files = [{"description": f.get("description"), "location": f.get("location")}
                     for f in struct.get("in_network_files", [])]
            oon = struct.get("allowed_amount_file")
            matches.append({"plans": hit, "in_network_files": files,
                            "allowed_amount_file": oon})
            for p in hit:
                print(f"PLAN: {p.get('plan_name')} | id={p.get('plan_id')} "
                      f"({p.get('plan_id_type')}) | {p.get('plan_market_type')}")
            for f in files:
                print(f"   IN-NETWORK: {f['description']}\n      {f['location']}")
            if oon:
                print(f"   OUT-OF-NETWORK: {oon.get('location')}")
            print()
    print(f"{len(matches)} matching reporting_structure entries", file=sys.stderr)
    if args.out:
        with open(args.out, "w") as out:
            json.dump(matches, out, indent=2)


def cmd_rates(args):
    path = resolve(args.url, args.file)
    codes = set(args.codes)
    prov_map = {}
    if not args.skip_provider_refs:
        with open_local(path) as fh:
            for ref in ijson.items(fh, "provider_references.item"):
                prov_map[ref.get("provider_group_id")] = ref.get("provider_groups", [])

    n = 0
    with open(args.out, "w", newline="") as out, open_local(path) as fh:
        w = csv.writer(out)
        w.writerow(["billing_code_type", "billing_code", "description", "arrangement",
                    "provider_group_id", "npi", "tin_type", "tin", "negotiated_type",
                    "negotiated_rate", "billing_class", "service_code", "modifier",
                    "expiration_date"])
        for item in ijson.items(fh, "in_network.item", use_float=True):
            if codes and str(item.get("billing_code")) not in codes:
                continue
            for nr in item.get("negotiated_rates", []):
                groups = [(None, g) for g in nr.get("provider_groups", [])]
                for rid in nr.get("provider_references", []):
                    groups += [(rid, g) for g in prov_map.get(rid, [])]
                if not groups:
                    groups = [(None, {"npi": [None], "tin": {}})]
                for price in nr.get("negotiated_prices", []):
                    for rid, g in groups:
                        tin = g.get("tin") or {}
                        for npi in g.get("npi") or [None]:
                            w.writerow([item.get("billing_code_type"), item.get("billing_code"),
                                        item.get("description"),
                                        item.get("negotiation_arrangement"), rid, npi,
                                        tin.get("type"), tin.get("value"),
                                        price.get("negotiated_type"),
                                        price.get("negotiated_rate"),
                                        price.get("billing_class"),
                                        "|".join(price.get("service_code") or []),
                                        "|".join(price.get("billing_code_modifier") or []),
                                        price.get("expiration_date")])
                            n += 1
    print(f"wrote {n} rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("count", "toc"):
        p = sub.add_parser(name)
        p.add_argument("--url", default=TOC_URL)
        p.add_argument("--file", help="use an already-downloaded TOC instead")
        if name == "toc":
            p.add_argument("--search", required=True, help="plan name substring, EIN, or HIOS id")
            p.add_argument("--out")
    r = sub.add_parser("rates")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--file")
    r.add_argument("--codes", nargs="*", default=[], help="billing codes; omit for all (huge)")
    r.add_argument("--out", default="rates.csv")
    r.add_argument("--skip-provider-refs", action="store_true")
    a = ap.parse_args()
    {"count": cmd_count, "toc": cmd_toc, "rates": cmd_rates}[a.cmd](a)
