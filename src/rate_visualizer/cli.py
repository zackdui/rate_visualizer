"""Command line: rate-visualizer <step> --config configs/tx.toml"""
import argparse, concurrent.futures, http.client, os, sys, time
from collections import Counter
import ijson, pyarrow.parquet as pq, requests, urllib3
from . import build_db, config, extract, index, io, nppes, profile


def cmd_index(cfg, args):
    out, t = index.run(cfg)
    m = t["index_meta"][0]
    files = t["index_files"]
    kinds = Counter(f["file_kind"] for f in files)
    in_net = [f for f in files if f["file_kind"] == "in_network"]
    print(f"index {m['index_source']}  last_updated_on={m['last_updated_on']}  version={m['version']}")
    print(f"  reporting_structure entries : {m['n_reporting_structures']}")
    print(f"  plan rows                   : {m['n_plan_rows']}")
    print(f"  file references             : {m['n_file_references']}  "
          f"(in_network {sum(p['file_kind'] == 'in_network' for p in t['plan_files'])}, "
          f"allowed_amount {sum(p['file_kind'] == 'allowed_amount' for p in t['plan_files'])})")
    print(f"  unique files                : {dict(kinds)}")
    print(f"  in-network in state         : {sum(f['is_in_state_file'] for f in in_net)} of {len(in_net)}")
    print(f"  files with >1 description   : {sum(len(f['descriptions']) > 1 for f in files)}")
    print(f"  anomalies                   : {dict(Counter(a['type'] for a in t['anomalies']))}")
    print(f"wrote {out}/")


RETRYABLE = (requests.RequestException, urllib3.exceptions.HTTPError, http.client.HTTPException,
             EOFError, OSError, ijson.common.IncompleteJSONError)


def latest_index_date(cfg):
    root = os.path.join(cfg.data_dir, cfg.state)
    dates = sorted(d for d in (os.listdir(root) if os.path.isdir(root) else [])
                   if os.path.exists(os.path.join(root, d, "index", "index_files.parquet")))
    if not dates:
        sys.exit(f"no index output under {root}/ - run `rate-visualizer index` first")
    return dates[-1]


def select_files(cfg, args, index_dir):
    files = pq.read_table(os.path.join(index_dir, "index_files.parquet")).to_pylist()
    in_net = [f for f in files if f["file_kind"] == "in_network"]
    if args.local:
        name = os.path.basename(args.local)
        match = [f for f in in_net if f["file_name"] == name]
        if match:
            return [(match[0], args.local)]
        key = f"local:{name}"
        return [({"file_id": index.file_id_for(key), "file_key": key, "url": None, "file_name": name,
                  "is_in_state_file": cfg.in_state_filename_marker in name}, args.local)]
    if args.file_id:
        by_id = {f["file_id"]: f for f in in_net}
        unknown = [i for i in args.file_id if i not in by_id]
        if unknown:
            sys.exit(f"unknown in-network file_id(s): {unknown}")
        return [(by_id[i], None) for i in args.file_id]
    scope = "all" if args.all else "in_state" if args.in_state else cfg.scope
    chosen = in_net if scope == "all" else [f for f in in_net if f["is_in_state_file"]]
    return [(f, None) for f in sorted(chosen, key=lambda f: f["file_name"])]


def extract_one(cfg, f, local, out_root, label, limits):
    """Extract one file with retries. Returns (ok, log lines). Runs in its own worker process."""
    lines, attempts = [], 1 if local else cfg.retries
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            m = extract.extract_file(cfg, f["file_id"], f["file_key"], out_root,
                                     url=None if local else f["url"], path=local, limits=limits)
        except extract.BudgetExceeded as e:  # not retried: it would hit the same limit again
            lines.append(f"{label}: STOPPED, over limit: {e}")
            return False, lines
        except RETRYABLE as e:
            lines.append(f"{label}: attempt {attempt}/{attempts} failed: {type(e).__name__}: {e}")
            continue
        lines.append(f"{label}: {m['compressed_bytes'] / 1e6:,.1f} MB in {time.time() - t0:,.0f}s | "
                     f"items {m['n_in_network_items']:,} matched {m['n_items_matched']} | "
                     f"prices {m['n_price_rows']:,} rate rows {m['n_rate_rows']:,} | "
                     f"groups {m['n_groups_referenced']:,} npi rows {m['n_provider_group_npi_rows_kept']:,} | "
                     f"anomalies {m['n_anomalies']} | peak RAM {m['peak_rss_mb']:,.0f} MB")
        return True, lines
    return False, lines


def cmd_extract(cfg, args):
    index_date = args.index_date or latest_index_date(cfg)
    base = io.run_dir(cfg, index_date)
    out_root = os.path.join(base, "files")
    todo = select_files(cfg, args, os.path.join(base, "index"))
    workers = max(1, args.workers)
    limits = extract.Limits(max_rss_bytes=int(cfg.max_memory_gb / workers * 2**30), storage_root=cfg.data_dir,
                            max_storage_bytes=int(cfg.max_storage_gb * 2**30))
    used = extract.dir_size(cfg.data_dir) / 2**30
    print(f"extract: {len(todo)} file(s) -> {out_root}/  (workers={workers}, RAM limit "
          f"{cfg.max_memory_gb / workers:.1f} GB per worker / {cfg.max_memory_gb:g} GB total, storage "
          f"{used:.2f} of {cfg.max_storage_gb:g} GB used)", flush=True)
    if used > cfg.max_storage_gb:
        sys.exit(f"storage limit already exceeded: {cfg.data_dir} is {used:.2f} GB")
    jobs = []
    for n, (f, local) in enumerate(todo, 1):
        label = f"[{n}/{len(todo)}] {f['file_id']} {f['file_name']}"
        if extract.is_done(out_root, f["file_id"]) and not args.force:
            print(f"{label}: already done (use --force to redo)")
        else:
            jobs.append((f, local, label))
    failed, t0 = [], time.time()
    # A fresh process per file, so each file's peak-memory measurement starts from zero.
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=1) as pool:
        futures = {pool.submit(extract_one, cfg, f, local, out_root, label, limits): f
                   for f, local, label in jobs}
        for fut in concurrent.futures.as_completed(futures):
            ok, lines = fut.result()
            print("\n".join(lines), flush=True)
            if not ok:
                failed.append(futures[fut]["file_id"])
    print(f"extract finished in {time.time() - t0:,.0f}s: {len(jobs) - len(failed)} ok, {len(failed)} failed")
    if failed:
        sys.exit(f"failed file_id(s): {sorted(failed)}")


def cmd_profile(cfg, args):
    text, out = profile.run(cfg, args.index_date or latest_index_date(cfg))
    print(text)
    print(f"\nsaved {out}")


def cmd_build_db(cfg, args):
    t0 = time.time()
    print(f"build-db: DuckDB memory limit {cfg.max_memory_gb / 2:g} GB (half of max_memory_gb), 4 threads", flush=True)
    path, counts = build_db.build(cfg, args.index_date or latest_index_date(cfg),
                                  log=lambda msg: print(msg, flush=True))
    print(f"built {path} in {time.time() - t0:,.0f}s ({counts.pop('db_mb'):,.1f} MB)")
    for name, n in counts.items():
        print(f"  {name:22s} {n:>14,}")
    if counts["rates_without_group"]:
        print("WARNING: some rates reference provider groups that are missing (see anomalies)")


def cmd_nppes(cfg, args):
    t0 = time.time()
    meta, anomalies = nppes.run(cfg, args.index_date or latest_index_date(cfg), nppes_zip=args.nppes_zip,
                                nucc_csv=args.nucc_csv, log=lambda msg: print(msg, flush=True))
    print(f"nppes finished in {time.time() - t0:,.0f}s: {meta['n_found']:,} of {meta['n_npis_requested']:,} NPIs "
          f"found ({meta['n_not_found']:,} not in NPPES), {meta['n_deactivated']:,} deactivated, "
          f"{meta['n_without_specialty']:,} without a specialty")
    print(f"  anomalies: {dict(Counter(a['type'] for a in anomalies))}")


COMMANDS = {"index": cmd_index, "extract": cmd_extract, "profile": cmd_profile, "build-db": cmd_build_db,
            "nppes": cmd_nppes}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rate-visualizer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    parsers = {}
    for name in COMMANDS:
        parsers[name] = p = sub.add_parser(name)
        p.add_argument("--config", default="configs/tx.toml")
    parsers["nppes"].add_argument("--nppes-zip", help="use a local NPPES zip instead of finding/downloading it")
    parsers["nppes"].add_argument("--nucc-csv", help="use a local NUCC taxonomy CSV instead of finding/downloading it")
    for name in ("extract", "profile", "build-db", "nppes"):
        parsers[name].add_argument("--index-date", help="which index run to use (default: latest under data/<state>/)")
    e = parsers["extract"]
    pick = e.add_mutually_exclusive_group()
    pick.add_argument("--file-id", nargs="+", help="specific in-network file_id(s) from index_files")
    pick.add_argument("--in-state", action="store_true", help="all in-state in-network files")
    pick.add_argument("--all", action="store_true", help="every in-network file in the index")
    pick.add_argument("--local", help="parse a local copy; matched to the index by file name")
    e.add_argument("--force", action="store_true", help="redo files that already have _SUCCESS")
    e.add_argument("--workers", type=int, default=1, help="files processed in parallel (each uses ~1 GB RAM)")
    args = ap.parse_args(argv)
    COMMANDS[args.cmd](config.load(args.config), args)
