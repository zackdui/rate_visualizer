"""Steps 2-3: extract one in-network file in a single streaming pass.

Outputs (in <run_dir>/files/<file_id>/):
  rates.parquet            one row per (in_network[i].negotiated_rates[j].negotiated_prices[k] x provider_references[r])
                           for the configured codes (capitation items excluded)
  capitation.parquet       same, for every item with negotiation_arrangement "capitation" (any code), plus the
                           configured codes found in its covered_services
  capitation_covered_services.parquet  one row per covered_services entry of those items
  provider_groups.parquet  one row per NPI of every provider group the rates and capitation rows reference
  file_meta.parquet        one row: source identity (sha256, bytes), header values, counts
  anomalies.parquet        anything unexpected (unknown keys, undefined group ids, remote references, ...)
  _SUCCESS                 written last; its presence means the folder is complete
"""
import datetime, decimal, json, os, resource, shutil, subprocess, sys
import duckdb, pyarrow as pa, pyarrow.parquet as pq
from .io import open_source, write_parquet
from .index import ANOMALY_SCHEMA
from .jsonstream import iter_top_level

TOP_KEYS = {"reporting_entity_name", "reporting_entity_type", "plan_name", "plan_id_type", "plan_id",
            "plan_market_type", "plan_sponsor_name", "issuer_name", "last_updated_on", "version",
            "provider_references", "in_network"}
HEADER_KEYS = ["reporting_entity_name", "reporting_entity_type", "last_updated_on", "version"]
PROVIDER_REF_KEYS = {"provider_group_id", "network_name", "provider_groups", "location"}
PROVIDER_GROUP_KEYS = {"npi", "tin"}
TIN_KEYS = {"type", "value", "business_name"}
ITEM_KEYS = {"negotiation_arrangement", "name", "billing_code_type", "billing_code_type_version", "billing_code",
             "description", "negotiated_rates", "bundled_codes", "covered_services"}
RATE_KEYS = {"provider_references", "provider_groups", "negotiated_prices"}
PRICE_KEYS = {"negotiated_type", "negotiated_rate", "expiration_date", "service_code", "billing_class", "setting",
              "billing_code_modifier", "additional_information"}
COVERED_KEYS = {"billing_code_type", "billing_code_type_version", "billing_code", "description"}

GROUP_SCHEMA = pa.schema([
    ("file_id", pa.string()), ("m", pa.int64()), ("provider_group_id", pa.string()),
    ("network_name", pa.list_(pa.string())), ("n", pa.int64()), ("tin_type", pa.string()),
    ("tin_value", pa.string()), ("tin_business_name", pa.string()), ("p", pa.int64()), ("npi", pa.string())])
RATE_SCHEMA = pa.schema([
    ("file_id", pa.string()), ("i", pa.int64()), ("billing_code", pa.string()), ("billing_code_type", pa.string()),
    ("billing_code_type_version", pa.string()), ("name", pa.string()), ("description", pa.string()),
    ("negotiation_arrangement", pa.string()), ("bundled_codes", pa.string()), ("covered_services", pa.string()),
    ("j", pa.int64()), ("k", pa.int64()), ("r", pa.int64()), ("provider_group_id", pa.string()),
    ("negotiated_type", pa.string()), ("negotiated_rate", pa.float64()), ("negotiated_rate_raw", pa.string()),
    ("rate_unit", pa.string()), ("expiration_date", pa.string()), ("billing_class", pa.string()), ("setting", pa.string()),
    ("service_code", pa.list_(pa.string())), ("billing_code_modifier", pa.list_(pa.string())),
    ("additional_information", pa.string()), ("is_zero_rate", pa.bool_())])
# Same columns as rates, minus the (huge) covered_services JSON, plus which configured codes the payment covers.
CAP_SCHEMA = pa.schema([f for f in RATE_SCHEMA if f.name != "covered_services"]
                       + [("covered_target_codes", pa.list_(pa.string())), ("n_covered_services", pa.int64())])
COVERED_SCHEMA = pa.schema([
    ("file_id", pa.string()), ("i", pa.int64()), ("c", pa.int64()), ("billing_code_type", pa.string()),
    ("billing_code_type_version", pa.string()), ("billing_code", pa.string()), ("description", pa.string()),
    ("is_target_code", pa.bool_())])
META_SCHEMA = pa.schema([
    ("file_id", pa.string()), ("file_key", pa.string()), ("url", pa.string()), ("source", pa.string()),
    ("sha256", pa.string()), ("compressed_bytes", pa.int64())]
    + [(k, pa.string()) for k in HEADER_KEYS]
    + [("key_order", pa.list_(pa.string())), ("codes", pa.list_(pa.string())), ("code_type", pa.string()),
       ("n_provider_references", pa.int64()), ("n_provider_group_npi_rows_total", pa.int64()),
       ("n_in_network_items", pa.int64()), ("n_items_matched", pa.int64()), ("n_price_rows", pa.int64()),
       ("n_zero_price_rows", pa.int64()), ("n_rate_rows", pa.int64()), ("n_groups_referenced", pa.int64()),
       ("n_provider_group_npi_rows_kept", pa.int64()), ("n_capitation_items", pa.int64()),
       ("n_capitation_items_with_target_codes", pa.int64()), ("n_capitation_rows", pa.int64()),
       ("n_capitation_covered_services", pa.int64()), ("n_anomalies", pa.int64()),
       ("peak_rss_mb", pa.float64()), ("memory_limit_mb", pa.float64()), ("storage_limit_gb", pa.float64()),
       ("parser_git_commit", pa.string()), ("parser_git_dirty", pa.bool_()),
       ("started_at", pa.string()), ("finished_at", pa.string())])

# Temp file: one row per provider group with its NPIs as a list (unreferenced groups are dropped before exploding)
TEMP_GROUP_SCHEMA = pa.schema([f for f in GROUP_SCHEMA if f.name not in ("p", "npi")]
                              + [("npis", pa.list_(pa.string()))])
BATCH = 20_000
NPI_MIN, NPI_MAX = 1_000_000_000, 9_999_999_999

# What negotiated_rate means for each CMS negotiated_type (schema enum). Anything else -> "unknown" + anomaly.
RATE_UNITS = {
    "negotiated": "dollars",
    "derived": "dollars",
    "fee schedule": "dollars",
    "percentage": "percent_of_billed_charges",
    "per diem": "dollars_per_day",
}


class BudgetExceeded(Exception):
    """A memory or storage limit from the config was hit. The file's partial output is deleted."""


def peak_rss_bytes():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024  # macOS reports bytes, Linux kilobytes


def dir_size(root):
    total = 0
    for dirpath, _, names in os.walk(root):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass  # a file removed while walking
    return total


class Limits:
    """Stops a file when this process's peak memory or the output folder's size goes over the limit."""

    def __init__(self, max_rss_bytes=None, storage_root=None, max_storage_bytes=None):
        self.max_rss_bytes, self.storage_root, self.max_storage_bytes = max_rss_bytes, storage_root, max_storage_bytes

    def check_memory(self, where):
        if self.max_rss_bytes and peak_rss_bytes() > self.max_rss_bytes:
            raise BudgetExceeded(f"memory: peak {peak_rss_bytes() / 2**20:,.0f} MB > limit "
                                 f"{self.max_rss_bytes / 2**20:,.0f} MB (at {where})")

    def check_storage(self, where):
        if self.max_storage_bytes and self.storage_root:
            used = dir_size(self.storage_root)
            if used > self.max_storage_bytes:
                raise BudgetExceeded(f"storage: {self.storage_root} is {used / 2**30:,.2f} GB > limit "
                                     f"{self.max_storage_bytes / 2**30:,.2f} GB (at {where})")


def _str(v):
    return None if v is None else str(v)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def git_state():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True,
                                check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=here, capture_output=True,
                                    text=True, check=True).stdout.strip())
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


class _Extractor:
    def __init__(self, file_id, codes, code_type, tmp_dir, limits):
        self.file_id, self.codes, self.code_type, self.limits = file_id, set(codes), code_type, limits
        self.anomalies, self.referenced = [], set()
        self.group_buf, self.group_path, self.group_writer = [], os.path.join(tmp_dir, "_all_groups.parquet"), None
        self.rate_buf, self.rate_path, self.rate_writer = [], os.path.join(tmp_dir, "rates.parquet"), None
        self.n_refs = self.n_group_rows = self.n_items = self.n_matched = self.n_prices = self.n_zero_prices = 0
        self.n_rate_rows = self.n_cap_items = self.n_cap_items_with_targets = 0
        self.cap_rows, self.cov_rows = [], []

    def close_writers(self):
        for w in (self.group_writer, self.rate_writer):
            if w:
                w.close()
        self.group_writer = self.rate_writer = None

    def anomaly(self, type_, key, json_path, detail=""):
        self.anomalies.append({"step": "extract", "file_id": self.file_id, "type": type_, "key": _str(key),
                               "json_path": json_path, "detail": detail})

    def check_keys(self, obj, known, path):
        if not isinstance(obj, dict):
            self.anomaly("not_an_object", None, path, type(obj).__name__)
            return False
        for k in obj.keys() - known:
            self.anomaly("unknown_key", k, f"{path}.{k}")
        return True

    def str_list(self, v, path):
        if v is None:
            return None
        if not isinstance(v, list):
            self.anomaly("not_a_list", None, path, repr(v)[:200])
            v = [v]
        return [_str(x) for x in v]

    # ---- provider groups -------------------------------------------------------------------------------
    def add_group_rows(self, m, group_id, network_name, groups, path):
        for n, g in enumerate(groups or []):
            gpath = f"{path}.provider_groups[{n}]"
            if not self.check_keys(g, PROVIDER_GROUP_KEYS, gpath):
                continue
            tin = g.get("tin") or {}
            if self.check_keys(tin, TIN_KEYS, f"{gpath}.tin") and not tin:
                self.anomaly("missing_tin", "tin", gpath)
            npis = g.get("npi")
            if not isinstance(npis, list) or not npis:
                self.anomaly("missing_npi", "npi", gpath, repr(npis)[:200])
                npis = npis if isinstance(npis, list) else []
            for p, npi in enumerate(npis):
                if type(npi) is int and NPI_MIN <= npi <= NPI_MAX:
                    continue
                s = _str(npi)
                if not (s and len(s) == 10 and s.isdigit()):
                    self.anomaly("npi_not_10_digits", "npi", f"{gpath}.npi[{p}]", repr(npi))
            self.group_buf.append({
                "file_id": self.file_id, "m": m, "provider_group_id": group_id,
                "network_name": self.str_list(network_name, f"{path}.network_name"), "n": n,
                "tin_type": _str(tin.get("type")), "tin_value": _str(tin.get("value")),
                "tin_business_name": _str(tin.get("business_name")), "npis": [_str(x) for x in npis]})
            self.n_group_rows += len(npis)
            if len(self.group_buf) >= BATCH:
                self.flush_groups()

    def flush_groups(self):
        if not self.group_buf:
            return
        table = pa.Table.from_pylist(self.group_buf, schema=TEMP_GROUP_SCHEMA)
        if self.group_writer is None:
            self.group_writer = pq.ParquetWriter(self.group_path, TEMP_GROUP_SCHEMA, compression="zstd")
        self.group_writer.write_table(table)
        self.group_buf = []
        self.limits.check_storage("provider group flush")

    def flush_rates(self):
        if not self.rate_buf:
            return
        table = pa.Table.from_pylist(self.rate_buf, schema=RATE_SCHEMA)
        if self.rate_writer is None:
            self.rate_writer = pq.ParquetWriter(self.rate_path, RATE_SCHEMA, compression="zstd")
        self.rate_writer.write_table(table)
        self.rate_buf = []
        self.limits.check_storage("rate flush")

    def provider_reference(self, m, ref):
        self.n_refs += 1
        if self.n_refs % 1000 == 0:
            self.limits.check_memory(f"provider_references[{m}]")
        path = f"provider_references[{m}]"
        if not self.check_keys(ref, PROVIDER_REF_KEYS, path):
            return
        if "location" in ref:
            self.anomaly("remote_provider_reference", "location", path, _str(ref["location"]))
        gid = _str(ref.get("provider_group_id"))
        if gid is None:
            self.anomaly("missing_provider_group_id", "provider_group_id", path)
            return
        self.add_group_rows(m, gid, ref.get("network_name"), ref.get("provider_groups"), path)

    # ---- in_network ------------------------------------------------------------------------------------
    def in_network_item(self, i, item):
        self.n_items += 1
        path = f"in_network[{i}]"
        self.limits.check_memory(path)
        if not self.check_keys(item, ITEM_KEYS, path):
            return
        if item.get("negotiation_arrangement") == "capitation":  # fixed payment, not a per-visit price
            self.capitation_item(i, item, path)
            return
        if _str(item.get("billing_code")) not in self.codes or item.get("billing_code_type") != self.code_type:
            return
        self.n_matched += 1
        base = {**self.item_base(i, item), "covered_services": _json(item.get("covered_services"))}
        for row, group_ids in self.price_rows(i, item, path, base):
            self.n_prices += 1
            self.n_zero_prices += row["negotiated_rate"] == 0
            for r, gid in group_ids:
                self.rate_buf.append({**row, "r": r, "provider_group_id": gid})
                self.referenced.add(gid)
            self.n_rate_rows += len(group_ids)
            if len(self.rate_buf) >= BATCH:
                self.flush_rates()

    def capitation_item(self, i, item, path):
        """Every capitation item goes to capitation.parquet (whatever its code), with the configured codes it covers."""
        self.n_cap_items += 1
        covered = item.get("covered_services")
        if covered is not None and not isinstance(covered, list):
            self.anomaly("not_a_list", None, f"{path}.covered_services", repr(covered)[:200])
            covered = [covered]
        targets = set()
        for c, svc in enumerate(covered or []):
            cpath = f"{path}.covered_services[{c}]"
            if not self.check_keys(svc, COVERED_KEYS, cpath):
                continue
            code, ctype = _str(svc.get("billing_code")), _str(svc.get("billing_code_type"))
            is_target = code in self.codes and ctype == self.code_type
            if is_target:
                targets.add(code)
            self.cov_rows.append({"file_id": self.file_id, "i": i, "c": c, "billing_code_type": ctype,
                                  "billing_code_type_version": _str(svc.get("billing_code_type_version")),
                                  "billing_code": code, "description": _str(svc.get("description")),
                                  "is_target_code": is_target})
        if not covered:
            self.anomaly("capitation_without_covered_services", "covered_services", path)
        self.n_cap_items_with_targets += bool(targets)
        base = {**self.item_base(i, item), "covered_target_codes": sorted(targets),
                "n_covered_services": len(covered or [])}
        for row, group_ids in self.price_rows(i, item, path, base):
            for r, gid in group_ids:
                self.cap_rows.append({**row, "r": r, "provider_group_id": gid})
                self.referenced.add(gid)

    def item_base(self, i, item):
        return {"file_id": self.file_id, "i": i, "billing_code": _str(item.get("billing_code")),
                "billing_code_type": _str(item.get("billing_code_type")),
                "billing_code_type_version": _str(item.get("billing_code_type_version")),
                "name": _str(item.get("name")), "description": _str(item.get("description")),
                "negotiation_arrangement": _str(item.get("negotiation_arrangement")),
                "bundled_codes": _json(item.get("bundled_codes"))}

    def price_rows(self, i, item, path, base):
        """Yield (row, [(r, provider_group_id), ...]) for every negotiated_rates[j].negotiated_prices[k]."""
        for j, rate in enumerate(item.get("negotiated_rates") or []):
            rpath = f"{path}.negotiated_rates[{j}]"
            if not self.check_keys(rate, RATE_KEYS, rpath):
                continue
            group_ids = [(r, _str(g)) for r, g in enumerate(rate.get("provider_references") or [])]
            for n, g in enumerate(rate.get("provider_groups") or []):  # inline groups (schema-valid)
                gid = f"inline:{i}:{j}:{n}"
                self.add_group_rows(None, gid, None, [g], f"{rpath}.provider_groups[{n}]")
                group_ids.append((None, gid))
            if not group_ids:
                self.anomaly("rate_without_providers", None, rpath)
            prices = rate.get("negotiated_prices") or []
            if not prices:
                self.anomaly("rate_without_prices", None, rpath)
            for k, price in enumerate(prices):
                ppath = f"{rpath}.negotiated_prices[{k}]"
                if not self.check_keys(price, PRICE_KEYS, ppath):
                    continue
                raw = price.get("negotiated_rate")
                value = None
                if isinstance(raw, (int, decimal.Decimal)) and not isinstance(raw, bool):
                    value = float(raw)
                else:
                    self.anomaly("rate_not_a_number", "negotiated_rate", ppath, repr(raw)[:200])
                ntype = _str(price.get("negotiated_type"))
                unit = RATE_UNITS.get(ntype)
                if unit is None:
                    self.anomaly("unknown_negotiated_type", "negotiated_type", ppath, repr(ntype))
                    unit = "unknown"
                row = {**base, "j": j, "k": k, "negotiated_type": ntype,
                       "negotiated_rate": value, "negotiated_rate_raw": _str(raw), "rate_unit": unit,
                       "expiration_date": _str(price.get("expiration_date")),
                       "billing_class": _str(price.get("billing_class")), "setting": _str(price.get("setting")),
                       "service_code": self.str_list(price.get("service_code"), f"{ppath}.service_code"),
                       "billing_code_modifier": self.str_list(price.get("billing_code_modifier"),
                                                              f"{ppath}.billing_code_modifier"),
                       "additional_information": _str(price.get("additional_information")),
                       "is_zero_rate": value == 0 if value is not None else None}
                yield row, group_ids


def _json(v):
    return None if v is None else json.dumps(v, default=str, sort_keys=True)


def extract_file(cfg, file_id, file_key, out_root, url=None, path=None, limits=None):
    """Parse one file into <out_root>/<file_id>/. Returns the file_meta row.

    Raises on I/O, parse errors or BudgetExceeded; in every failure case the partial <file_id>.tmp/ is deleted.
    """
    final, tmp = os.path.join(out_root, file_id), os.path.join(out_root, file_id + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    limits = limits or Limits()
    ex = _Extractor(file_id, cfg.codes, cfg.code_type, tmp, limits)
    try:
        limits.check_storage("start")
        meta = _extract(cfg, ex, file_id, file_key, tmp, url, path, limits)
    except BaseException:
        ex.close_writers()
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    return meta


def _extract(cfg, ex, file_id, file_key, tmp, url, path, limits):
    started = _now()
    header, key_order = {}, []
    with open_source(url=url, path=path) as (stream, hasher):
        for kind, key, idx, value in iter_top_level(stream, {"provider_references", "in_network"}):
            if kind == "key":
                key_order.append(key)
                if key not in TOP_KEYS:
                    ex.anomaly("unknown_key", key, key)
            elif kind == "value":
                header[key] = value
            elif key == "provider_references":
                ex.provider_reference(idx, value)
            else:
                ex.in_network_item(idx, value)
        hasher.drain()
    ex.flush_groups()
    ex.flush_rates()
    ex.close_writers()
    if not os.path.exists(ex.rate_path):
        write_parquet([], RATE_SCHEMA, ex.rate_path)

    # Keep only the provider groups the extracted rates reference; record ids that were never defined.
    groups_out = os.path.join(tmp, "provider_groups.parquet")
    refs = pa.table({"provider_group_id": pa.array(sorted(ex.referenced), pa.string())})
    con = duckdb.connect()
    con.execute("SET threads = 1")
    con.execute("SET enable_progress_bar = false")
    con.execute(f"SET temp_directory = '{tmp}'")  # spills (if any) count toward the storage limit
    if limits.max_rss_bytes:
        con.execute(f"SET memory_limit = '{max(256, int(limits.max_rss_bytes / 2**20 / 2))}MB'")
    con.register("refs", refs)
    if os.path.exists(ex.group_path):
        con.execute(f"""COPY (
            SELECT file_id, m, provider_group_id, network_name, n, tin_type, tin_value, tin_business_name,
                   p - 1 AS p, npis[p] AS npi
            FROM (SELECT *, unnest(range(1, len(npis) + 1)) AS p
                  FROM read_parquet('{ex.group_path}') SEMI JOIN refs USING (provider_group_id))
            ORDER BY m NULLS LAST, provider_group_id, n, p)
            TO '{groups_out}' (FORMAT parquet, COMPRESSION zstd)""")
        defined = {r[0] for r in con.execute(
            f"SELECT DISTINCT provider_group_id FROM read_parquet('{ex.group_path}')").fetchall()}
        n_kept = con.execute(f"SELECT count(*) FROM read_parquet('{groups_out}')").fetchone()[0]
        os.remove(ex.group_path)
    else:
        write_parquet([], GROUP_SCHEMA, groups_out)
        defined, n_kept = set(), 0
    con.close()
    for gid in sorted(ex.referenced - defined):
        ex.anomaly("undefined_provider_group", gid, "in_network[*].negotiated_rates[*].provider_references")
    limits.check_memory("provider group filter")
    limits.check_storage("provider group filter")

    commit, dirty = git_state()
    meta = {"file_id": file_id, "file_key": file_key, "url": url, "source": url or os.path.abspath(path),
            "sha256": hasher.sha256.hexdigest(), "compressed_bytes": hasher.bytes,
            **{k: _str(header.get(k)) for k in HEADER_KEYS}, "key_order": key_order,
            "codes": sorted(cfg.codes), "code_type": cfg.code_type,
            "n_provider_references": ex.n_refs, "n_provider_group_npi_rows_total": ex.n_group_rows,
            "n_in_network_items": ex.n_items, "n_items_matched": ex.n_matched, "n_price_rows": ex.n_prices,
            "n_zero_price_rows": ex.n_zero_prices, "n_rate_rows": ex.n_rate_rows,
            "n_groups_referenced": len(ex.referenced), "n_provider_group_npi_rows_kept": n_kept,
            "n_capitation_items": ex.n_cap_items, "n_capitation_items_with_target_codes": ex.n_cap_items_with_targets,
            "n_capitation_rows": len(ex.cap_rows), "n_capitation_covered_services": len(ex.cov_rows),
            "n_anomalies": len(ex.anomalies), "peak_rss_mb": round(peak_rss_bytes() / 2**20, 1),
            "memory_limit_mb": limits.max_rss_bytes / 2**20 if limits.max_rss_bytes else None,
            "storage_limit_gb": limits.max_storage_bytes / 2**30 if limits.max_storage_bytes else None,
            "parser_git_commit": commit, "parser_git_dirty": dirty,
            "started_at": started, "finished_at": _now()}
    write_parquet(ex.cap_rows, CAP_SCHEMA, os.path.join(tmp, "capitation.parquet"))
    write_parquet(ex.cov_rows, COVERED_SCHEMA, os.path.join(tmp, "capitation_covered_services.parquet"))
    write_parquet(ex.anomalies, ANOMALY_SCHEMA, os.path.join(tmp, "anomalies.parquet"))
    write_parquet([meta], META_SCHEMA, os.path.join(tmp, "file_meta.parquet"))
    open(os.path.join(tmp, "_SUCCESS"), "w").close()
    return meta


def is_done(out_root, file_id):
    return os.path.exists(os.path.join(out_root, file_id, "_SUCCESS"))
