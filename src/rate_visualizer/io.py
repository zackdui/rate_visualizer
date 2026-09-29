"""File access shared by every step: downloads, local gzip detection, output paths."""
import contextlib, gzip, hashlib, io, os, sys
from urllib.parse import urlparse
import pyarrow as pa, pyarrow.parquet as pq, requests

DOWNLOAD_DIR = "mrf_data"


def download(url, dest_dir=DOWNLOAD_DIR):
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


class HashingReader:
    """Wraps a raw byte stream; hashes (SHA-256) and counts every byte read through it."""

    def __init__(self, raw):
        self.raw, self.sha256, self.bytes = raw, hashlib.sha256(), 0

    def readable(self):
        return True

    def read(self, n=-1):
        data = self.raw.read(n)
        self.sha256.update(data)
        self.bytes += len(data)
        return data

    def readinto(self, buf):
        data = self.read(len(buf))
        buf[:len(data)] = data
        return len(data)

    def drain(self):
        """Read to EOF so the hash covers the whole file (the JSON parser may stop before the gzip trailer)."""
        while self.read(1 << 20):
            pass

    @property
    def closed(self):
        return False


@contextlib.contextmanager
def open_source(url=None, path=None):
    """Open a remote URL (streamed, never saved) or a local file.

    Yields (decompressed binary stream, HashingReader). The hash is of the bytes exactly as served/stored
    (i.e. the .gz bytes). Call hasher.drain() after parsing to finish the hash.
    """
    if (url is None) == (path is None):
        raise ValueError("give exactly one of url or path")
    if path is not None:
        raw = open(path, "rb")
        close = raw.close
    else:
        r = requests.get(url, stream=True, timeout=300)
        r.raise_for_status()
        r.raw.decode_content = False  # hash the bytes as served
        raw, close = r.raw, r.close
    try:
        hasher = HashingReader(raw)
        buf = io.BufferedReader(hasher, buffer_size=1 << 20)
        stream = gzip.GzipFile(fileobj=buf) if buf.peek(2)[:2] == b"\x1f\x8b" else buf
        yield stream, hasher
    finally:
        close()


def file_key(location):
    """Identity of a remote file: its URL without the query string (drops expiring signatures)."""
    return location.split("?")[0]


def run_dir(cfg, index_date):
    return os.path.join(cfg.data_dir, cfg.state, index_date)


def write_parquet(rows, schema, path):
    """Write a list of dicts to Parquet with an explicit schema (so empty tables still have columns)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")
