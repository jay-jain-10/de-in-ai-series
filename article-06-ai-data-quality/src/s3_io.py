"""S3 helpers for the raw claims landing zone and staging between Airflow tasks."""

import json
import logging
from typing import List

import boto3

logger = logging.getLogger(__name__)


def load_claims_from_s3(bucket: str, prefix: str, region: str = "us-east-1") -> List[dict]:
    """Read newline-delimited or array JSON claim files under prefix."""
    s3 = boto3.client("s3", region_name=region)
    claims: List[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                # Newline-delimited JSON: one object per non-empty line.
                data = [json.loads(line) for line in body.splitlines() if line.strip()]
            claims.extend(data if isinstance(data, list) else [data])
    logger.info(f"Loaded {len(claims)} claims from s3://{bucket}/{prefix}")
    return claims


def write_json_to_s3(bucket: str, key: str, payload, region: str = "us-east-1") -> None:
    """Overwrite-by-key (idempotent) JSON write, used for the sample cache."""
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(payload, default=str).encode("utf-8"))
