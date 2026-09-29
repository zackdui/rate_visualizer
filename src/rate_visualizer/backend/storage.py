"""Object storage for site.duckdb and latest.json: Cloudflare R2 in production, a local folder for tests/offline use.

Both implement the same small interface, so publish and the loader don't care which one they get.
"""
import os, shutil


class StorageError(RuntimeError):
    pass


class LocalStore:
    """A folder that behaves like a bucket (keys are relative paths)."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _p(self, key):
        return os.path.join(self.root, *key.split("/"))

    def get_bytes(self, key):
        try:
            with open(self._p(key), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        os.makedirs(os.path.dirname(self._p(key)), exist_ok=True)
        with open(self._p(key), "wb") as fh:
            fh.write(data)

    def upload_file(self, path, key):
        os.makedirs(os.path.dirname(self._p(key)), exist_ok=True)
        shutil.copyfile(path, self._p(key))

    def download_file(self, key, path):
        if not os.path.exists(self._p(key)):
            raise StorageError(f"{key} not found in {self.root}")
        shutil.copyfile(self._p(key), path)

    def size(self, key):
        p = self._p(key)
        return os.path.getsize(p) if os.path.exists(p) else None

    def list(self, prefix):
        base = self._p(prefix)
        out = []
        for dirpath, _, names in os.walk(base):
            for n in names:
                out.append(os.path.relpath(os.path.join(dirpath, n), self.root).replace(os.sep, "/"))
        return sorted(out)

    def delete(self, keys):
        for k in keys:
            if os.path.exists(self._p(k)):
                os.remove(self._p(k))


class R2Store:
    """Cloudflare R2 through its S3-compatible API (boto3)."""

    def __init__(self, account_id, access_key_id, secret_access_key, bucket):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                               aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
                               region_name="auto", config=Config(retries={"max_attempts": 5, "mode": "standard"}))

    def get_bytes(self, key):
        from botocore.exceptions import ClientError
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise StorageError(f"R2 get {key}: {e}") from e

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def upload_file(self, path, key):
        from boto3.s3.transfer import TransferConfig
        # multipart in 64 MB parts (a ~350 MB file is 6 parts)
        self.s3.upload_file(path, self.bucket, key, Config=TransferConfig(multipart_chunksize=64 * 2**20))

    def download_file(self, key, path):
        self.s3.download_file(self.bucket, key, path)

    def size(self, key):
        from botocore.exceptions import ClientError
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError:
            return None

    def list(self, prefix):
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                return sorted(keys)
            token = resp["NextContinuationToken"]

    def delete(self, keys):
        for i in range(0, len(keys), 1000):
            chunk = keys[i:i + 1000]
            if chunk:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in chunk]})


def store_from_settings(settings, local_root=None):
    """LocalStore if local_root is given, else R2 (with a clear error listing any missing variables)."""
    if local_root:
        return LocalStore(local_root)
    missing = settings.missing_r2()
    if missing:
        raise StorageError(f"R2 is not configured: set {', '.join(missing)} in .env (laptop) or Render's "
                           "environment variables (see .env.example)")
    return R2Store(settings.r2_account_id, settings.r2_access_key_id, settings.r2_secret_access_key, settings.r2_bucket)
