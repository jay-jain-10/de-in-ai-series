"""Cost levers from Part 3: pre-filter, model tiering, file-hash dedup."""
import logging
import hashlib
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")


def pre_filter(event, context):
    """
    Quick filter before expensive processing.
    Returns True if document should enter the pipeline.
    """
    s3 = boto3.client("s3")
    key = event["detail"]["object"]["key"]
    bucket = event["detail"]["bucket"]["name"]

    # Check S3 metadata tags
    tags = s3.get_object_tagging(Bucket=bucket, Key=key)
    tag_dict = {t["Key"]: t["Value"] for t in tags["TagSet"]}

    doc_type = tag_dict.get("document_type", "unknown")

    if doc_type in ("receipt", "packing_slip", "delivery_note"):
        # Log it, skip it
        logger.info(f"Skipped non-invoice: {key} (tagged as {doc_type})")
        return {"process": False, "reason": f"Tagged as {doc_type}"}

    return {"process": True, "bucket": bucket, "key": key}


def choose_extraction_model(textract_confidence: float, page_count: int) -> str:
    """Pick the right model based on document complexity."""
    if textract_confidence > 0.90 and page_count == 1:
        return "anthropic.claude-3-haiku-20240307-v1:0"
    else:
        return "anthropic.claude-3-5-sonnet-20241022-v2:0"




def compute_file_hash(bucket: str, key: str) -> str:
    """SHA-256 hash of file content for dedup."""
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    content = obj["Body"].read()
    return hashlib.sha256(content).hexdigest()


def is_duplicate(file_hash: str) -> bool:
    """Check if we've already processed this exact file."""
    table = dynamodb.Table("processed-invoices")
    response = table.query(
        IndexName="file-hash-index",
        KeyConditionExpression=Key("file_hash").eq(file_hash),
    )
    return response["Count"] > 0
