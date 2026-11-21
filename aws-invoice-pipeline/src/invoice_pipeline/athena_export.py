"""Nightly DynamoDB -> S3 Parquet export so finance can query via Athena (Part 3).
Requires: pip install pandas pyarrow s3fs"""
from datetime import datetime
from decimal import Decimal
import boto3

dynamodb = boto3.resource("dynamodb", region_name="us-east-1")


# The nightly export Lambda we had to build
# (This is the rework I mentioned)
def export_to_s3(event, context):
    """Export DynamoDB table to S3 as Parquet for Athena."""
    import pandas as pd

    table = dynamodb.Table("processed-invoices")

    # Full scan — fine for 4K records, terrible at scale
    items = []
    response = table.scan()
    items.extend(response["Items"])
    while "LastEvaluatedKey" in response:
        response = table.scan(
            ExclusiveStartKey=response["LastEvaluatedKey"]
        )
        items.extend(response["Items"])

    df = pd.DataFrame(items)

    # Convert Decimal to float for Parquet
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].apply(
            lambda x: float(x) if isinstance(x, Decimal) else x
        )

    # Write to S3 as Parquet
    # Note: requires s3fs and pyarrow — pip install s3fs pyarrow
    s3_path = f"s3://invoices-analytics/{datetime.now().strftime('%Y/%m/%d')}/invoices.parquet"
    df.to_parquet(s3_path, index=False)

    return {"exported": len(items), "path": s3_path}
