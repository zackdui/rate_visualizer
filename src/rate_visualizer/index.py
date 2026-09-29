"""Step 1: read the index (table of contents) into index_meta, index_files, index_plans, plan_files, anomalies."""
import hashlib, os
from urllib.parse import urlparse
import pyarrow as pa
from .io import download, open_local, file_key, run_dir, write_parquet
from .jsonstream import iter_top_level

TOP_KEYS = {"reporting_entity_name", "reporting_entity_type", "version", "last_updated_on", "reporting_structure"}
STRUCTURE_KEYS = {"reporting_plans", "in_network_files", "allowed_amount_file"}
PLAN_KEYS = ["plan_name", "issuer_name", "plan_id_type", "plan_id", "plan_market_type", "plan_sponsor_name"]
FILE_KEYS = {"description", "location"}
FILE_LISTS = [("in_network_files", "in_network"), ("allowed_amount_file", "allowed_amount")]

ANOMALY_SCHEMA = pa.schema([("step", pa.string()), ("file_id", pa.string()), ("type", pa.string()),
                            ("key", pa.string()), ("json_path", pa.string()), ("detail", pa.string())])
META_SCHEMA = pa.schema([("state", pa.string()), ("index_source", pa.string()), ("index_sha256", pa.string()),
                         ("index_bytes", pa.int64()), ("config_source", pa.string()),
                         ("reporting_entity_name", pa.string()), ("reporting_entity_type", pa.string()),
                         ("version", pa.string()), ("last_updated_on", pa.string()),
                         ("key_order", pa.list_(pa.string())), ("n_reporting_structures", pa.int64()),
                         ("n_plan_rows", pa.int64()), ("n_file_references", pa.int64()),
                         ("n_unique_files", pa.int64()), ("n_anomalies", pa.int64())])
PLAN_SCHEMA = pa.schema([("s", pa.int64()), ("q", pa.int64())] + [(k, pa.string()) for k in PLAN_KEYS])
PLAN_FILE_SCHEMA = pa.schema([("s", pa.int64()), ("f", pa.int64()), ("file_kind", pa.string()),
                              ("file_id", pa.string()), ("file_key", pa.string()), ("description", pa.string())])
FILE_SCHEMA = pa.schema([("file_id", pa.string()), ("file_key", pa.string()), ("url", pa.string()),
                         ("file_name", pa.string()), ("host", pa.string()), ("file_kind", pa.string()),
                         ("descriptions", pa.list_(pa.string())), ("is_in_state_file", pa.bool_()),
                         ("n_references", pa.int64()), ("state", pa.string()), ("index_date", pa.string())])


def file_id_for(key):
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _str(v):
    return None if v is None else str(v)


def parse_index(path, cfg):
    """Parse an index file. Returns a dict of table name -> list of rows (no files written)."""
    meta, key_order = {}, []
    plans, plan_files, anomalies, files = [], [], [], {}

    def anomaly(type_, key, json_path, detail=""):
        anomalies.append({"step": "index", "file_id": None, "type": type_, "key": key,
                          "json_path": json_path, "detail": detail})

    n_structures = 0
    with open_local(path) as fh:
        for kind, key, s, value in iter_top_level(fh, {"reporting_structure"}):
            if kind == "key":
                key_order.append(key)
                if key not in TOP_KEYS:
                    anomaly("unknown_key", key, key)
                continue
            if kind == "value":
                meta[key] = value
                continue
            n_structures += 1
            for k in set(value) - STRUCTURE_KEYS:
                anomaly("unknown_key", k, f"reporting_structure[{s}].{k}")
            for q, plan in enumerate(value.get("reporting_plans") or []):
                for k in set(plan) - set(PLAN_KEYS):
                    anomaly("unknown_key", k, f"reporting_structure[{s}].reporting_plans[{q}].{k}")
                plans.append({"s": s, "q": q, **{k: _str(plan.get(k)) for k in PLAN_KEYS}})
            for list_key, file_kind in FILE_LISTS:
                entries = value.get(list_key)
                if entries is None:
                    continue
                if isinstance(entries, dict):  # the schema says object; BCBSTX publishes a list
                    entries = [entries]
                for f, entry in enumerate(entries):
                    path_ = f"reporting_structure[{s}].{list_key}[{f}]"
                    for k in set(entry) - FILE_KEYS:
                        anomaly("unknown_key", k, f"{path_}.{k}")
                    location = entry.get("location")
                    if not location:
                        anomaly("blank_location", "location", path_, repr(entry))
                        continue
                    key_ = file_key(location)
                    fid = file_id_for(key_)
                    plan_files.append({"s": s, "f": f, "file_kind": file_kind, "file_id": fid,
                                       "file_key": key_, "description": entry.get("description")})
                    row = files.get(key_)
                    if row is None:
                        name = os.path.basename(urlparse(key_).path)
                        row = files[key_] = {
                            "file_id": fid, "file_key": key_, "url": location, "file_name": name,
                            "host": urlparse(key_).netloc, "file_kind": file_kind, "descriptions": set(),
                            "is_in_state_file": cfg.in_state_filename_marker in name, "n_references": 0,
                            "state": cfg.state, "index_date": None}
                    elif row["file_kind"] != file_kind:
                        anomaly("file_kind_conflict", key_, path_, f"{row['file_kind']} vs {file_kind}")
                    row["n_references"] += 1
                    if entry.get("description") is not None:
                        row["descriptions"].add(entry["description"])

    index_date = _str(meta.get("last_updated_on"))
    if not index_date:
        raise ValueError(f"{path}: index has no last_updated_on")
    file_rows = []
    for row in files.values():
        row["descriptions"] = sorted(row["descriptions"])
        row["index_date"] = index_date
        file_rows.append(row)
    meta_row = {"state": cfg.state, "index_source": str(path), "index_sha256": sha256_of(path),
                "index_bytes": os.path.getsize(path), "config_source": cfg.source,
                **{k: _str(meta.get(k)) for k in ("reporting_entity_name", "reporting_entity_type",
                                                   "version", "last_updated_on")},
                "key_order": key_order, "n_reporting_structures": n_structures, "n_plan_rows": len(plans),
                "n_file_references": len(plan_files), "n_unique_files": len(file_rows),
                "n_anomalies": len(anomalies)}
    return {"index_meta": [meta_row], "index_files": file_rows, "index_plans": plans,
            "plan_files": plan_files, "anomalies": anomalies}


SCHEMAS = {"index_meta": META_SCHEMA, "index_files": FILE_SCHEMA, "index_plans": PLAN_SCHEMA,
           "plan_files": PLAN_FILE_SCHEMA, "anomalies": ANOMALY_SCHEMA}


def run(cfg):
    path = cfg.index_path if cfg.index_path else download(cfg.index_url)
    tables = parse_index(path, cfg)
    out = os.path.join(run_dir(cfg, tables["index_meta"][0]["last_updated_on"]), "index")
    for name, rows in tables.items():
        write_parquet(rows, SCHEMAS[name], os.path.join(out, f"{name}.parquet"))
    return out, tables
