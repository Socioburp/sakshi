"""Cloudflare R2.

Two things the rest of the code depends on:

* `public_url()` returns a URL reachable by an anonymous client on the open
  internet. Instagram's container endpoint FETCHES the image; a signed-URL-only
  bucket fails at publish time, not at upload time.
* Draft objects go under `drafts/` and are expected to be swept by the bucket
  lifecycle rule (expire after 7 days). Approved creatives are copied to
  `published/`, which has no expiry.
"""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import UTC, datetime
from functools import lru_cache

import boto3
from botocore.config import Config

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

DRAFT_PREFIX = "drafts"
PUBLISHED_PREFIX = "published"


@lru_cache(maxsize=1)
def _s3():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def key_for(brand_id: str, creative_id: str, suffix: str, *, draft: bool = True) -> str:
    day = datetime.now(UTC).strftime("%Y/%m/%d")
    prefix = DRAFT_PREFIX if draft else PUBLISHED_PREFIX
    return f"{prefix}/{day}/{brand_id}/{creative_id}-{suffix}"


def put(key: str, data: bytes, content_type: str | None = None) -> str:
    ct = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
    _s3().put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=data,
        ContentType=ct,
        CacheControl="public, max-age=31536000, immutable",
        Metadata={"sha256": hashlib.sha256(data).hexdigest()[:32]},
    )
    url = public_url(key)
    log.info("r2_put", key=key, bytes=len(data), url=url)
    return url


def public_url(key: str) -> str:
    base = settings.r2_public_base_url.rstrip("/")
    if not base:
        raise RuntimeError(
            "R2_PUBLIC_BASE_URL is unset. Instagram fetches image_url anonymously, "
            "so the bucket needs a public dev subdomain or a custom domain."
        )
    return f"{base}/{key}"


def promote(draft_key: str) -> str:
    """Copy an approved draft out of the expiring prefix."""
    published_key = draft_key.replace(f"{DRAFT_PREFIX}/", f"{PUBLISHED_PREFIX}/", 1)
    _s3().copy_object(
        Bucket=settings.r2_bucket,
        Key=published_key,
        CopySource={"Bucket": settings.r2_bucket, "Key": draft_key},
        MetadataDirective="COPY",
    )
    return public_url(published_key)


LIFECYCLE_RULE = {
    "Rules": [
        {
            "ID": "expire-unapproved-drafts",
            "Status": "Enabled",
            "Filter": {"Prefix": f"{DRAFT_PREFIX}/"},
            "Expiration": {"Days": 7},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
        }
    ]
}


def apply_lifecycle() -> None:
    """Idempotent. Run once after creating the bucket: python -m app.integrations.storage.r2"""
    _s3().put_bucket_lifecycle_configuration(
        Bucket=settings.r2_bucket, LifecycleConfiguration=LIFECYCLE_RULE
    )
    log.info("r2_lifecycle_applied", bucket=settings.r2_bucket, rule="expire drafts after 7d")


if __name__ == "__main__":
    apply_lifecycle()
    print(f"lifecycle rule applied to {settings.r2_bucket}: drafts/ expire after 7 days")


def get(key: str) -> bytes:
    """Fetch an object back. Used by re-composite, which reuses the background
    instead of paying the image model again."""
    obj = _s3().get_object(Bucket=settings.r2_bucket, Key=key)
    return obj["Body"].read()
