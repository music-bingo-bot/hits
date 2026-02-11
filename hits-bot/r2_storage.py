# r2_storage.py
import os
import mimetypes
from typing import Optional, Tuple, Dict, Any

import boto3
from botocore.config import Config


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def r2_enabled() -> bool:
    return bool(
        _env("R2_BUCKET")
        and _env("R2_ENDPOINT")
        and _env("R2_ACCESS_KEY_ID")
        and _env("R2_SECRET_ACCESS_KEY")
    )


def get_r2_client():
    endpoint = _env("R2_ENDPOINT")
    access_key = _env("R2_ACCESS_KEY_ID")
    secret_key = _env("R2_SECRET_ACCESS_KEY")

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _guess_content_type(name: str) -> Optional[str]:
    ct, _ = mimetypes.guess_type(name)
    return ct


def _r2_prefix() -> str:
    return _env("R2_PREFIX", "").strip().strip("/")


def build_r2_key(subdir: str, filename: str) -> str:
    """
    Deterministic key (no random tokens):
      <prefix>/<subdir>/<filename>
    """
    prefix = _r2_prefix()
    subdir = (subdir or "").strip().strip("/")
    filename = os.path.basename(filename or "file.bin").replace("\\", "_").replace("/", "_")

    parts = [p for p in [prefix, subdir, filename] if p]
    return "/".join(parts)


def put_bytes_to_r2(body: bytes, key: str, content_type: Optional[str] = None) -> None:
    if not r2_enabled():
        raise RuntimeError("R2 is not configured (missing env vars).")

    bucket = _env("R2_BUCKET")
    client = get_r2_client()

    if not content_type:
        content_type = _guess_content_type(key)

    extra = {}
    if content_type:
        extra["ContentType"] = content_type

    client.put_object(Bucket=bucket, Key=key, Body=body, **extra)


def overwrite_bytes_in_r2(body: bytes, key: str, content_type: Optional[str] = None) -> None:
    # Same as put_object; semantic name for "replace"
    put_bytes_to_r2(body, key, content_type=content_type)


def get_bytes_from_r2(key: str) -> Tuple[bytes, Dict[str, Any]]:
    if not r2_enabled():
        raise RuntimeError("R2 is not configured (missing env vars).")

    bucket = _env("R2_BUCKET")
    client = get_r2_client()

    resp = client.get_object(Bucket=bucket, Key=key)
    data = resp["Body"].read()

    meta = {
        "ContentType": resp.get("ContentType"),
        "ContentLength": resp.get("ContentLength"),
        "ETag": resp.get("ETag"),
    }
    return data, meta


def presign_get_url(
    key: str,
    expires_seconds: int = 3600,
    download_filename: Optional[str] = None,
    content_type: Optional[str] = None,
) -> str:
    if not r2_enabled():
        raise RuntimeError("R2 is not configured (missing env vars).")

    bucket = _env("R2_BUCKET")
    client = get_r2_client()

    params = {"Bucket": bucket, "Key": key}
    if download_filename:
        params["ResponseContentDisposition"] = f'inline; filename="{download_filename}"'
    if content_type:
        params["ResponseContentType"] = content_type

    return client.generate_presigned_url(
        ClientMethod="get_object",
        Params=params,
        ExpiresIn=expires_seconds,
    )


def normalize_r2_audio_ref(file_field: str) -> Optional[str]:
    """
    Audio in DB can be:
      - "r2:<key>"
      - legacy local "uploads/..."
      - telegram file_id
    Returns <key> if it's r2, else None.
    """
    if not file_field:
        return None
    ff = file_field.strip()
    if ff.startswith("r2:"):
        return ff[3:]
    if ff.startswith("r2/"):
        return ff[3:]
    return None


def normalize_r2_hint_ref(hint_field: str) -> Optional[str]:
    """
    Hint image in DB can be:
      - "r2/<key>"   (used by templates: src="/{{ hint }}")
      - legacy local "uploads/..."
      - telegram file_id / url (rare)
    Returns <key> if it's r2/<key>, else None.
    """
    if not hint_field:
        return None
    ff = hint_field.strip()
    if ff.startswith("r2/"):
        return ff[3:]
    if ff.startswith("r2:"):
        return ff[3:]
    return None
