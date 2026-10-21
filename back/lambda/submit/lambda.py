"""POST /v1/reviews/analyze: validate the Review Schema 1.1 request, charge the daily quota, queue the analysis.

Request header x-trustlens-client: an install ID the extension generates once (not a secret; it only keys the quota).
Responses: 202 {analysisId, status, quota_remaining, poll_after_seconds, estimated_seconds}
           200 same shape with reused=true when this client analysed the same product in the last REUSE_HOURS
           400 INVALID_REQUEST, 429 QUOTA_EXCEEDED
"""
import datetime
import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
S3 = boto3.client("s3")
LAMBDA = boto3.client("lambda")
DAILY_QUOTA = int(os.environ.get("DAILY_QUOTA", "10"))
REUSE_HOURS = int(os.environ.get("REUSE_HOURS", "24"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
KST = datetime.timezone(datetime.timedelta(hours=9))

MAX_REVIEWS, MAX_PER_RATING, MAX_BODY, MAX_TOTAL = 500, 100, 2000, 100_000  # spec 7.1 request limits


def reply(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, ensure_ascii=False)}


def invalid(message):
    return reply(400, {"error": "INVALID_REQUEST", "message": message})


def validate(doc):
    """Return an error message, or None when the document is acceptable."""
    if not isinstance(doc, dict) or doc.get("schema_version") != "1.1" or doc.get("site") != "coupang":
        return "schema_version 1.1 and site coupang are required"
    if not str((doc.get("product") or {}).get("product_id") or "").isdigit():
        return "product.product_id is required"
    reviews = doc.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        return "reviews must be a non-empty list"
    if len(reviews) > MAX_REVIEWS:
        return f"at most {MAX_REVIEWS} reviews"
    per_rating, total = {}, 0
    for r in reviews:
        body = r.get("body") if isinstance(r, dict) else None
        if not isinstance(body, str) or not body.strip() or not r.get("review_id"):
            return "every review needs review_id and a non-empty body"
        if r.get("rating") not in (None, 1, 2, 3, 4, 5):
            return "rating must be 1-5 or null"
        if len(body) > MAX_BODY:
            return f"review body over {MAX_BODY} characters"
        total += len(body)
        per_rating[r.get("rating")] = per_rating.get(r.get("rating"), 0) + 1
    if total > MAX_TOTAL:
        return f"bodies over {MAX_TOTAL} characters in total"
    if any(n > MAX_PER_RATING for k, n in per_rating.items() if k):
        return f"at most {MAX_PER_RATING} reviews per rating"
    if not isinstance((doc.get("collection") or {}).get("by_rating"), list):
        return "collection.by_rating is required"
    return None


def quota_key(client):
    return f"quota#{client}#{datetime.datetime.now(KST):%Y%m%d}"


def remaining(client):
    item = TABLE.get_item(Key={"recordId": quota_key(client)}).get("Item") or {}
    return max(DAILY_QUOTA - int(item.get("used", 0)), 0)


def charge(client, now):
    """Atomically take one analysis from today's quota. False when it is used up."""
    try:
        TABLE.update_item(
            Key={"recordId": quota_key(client)},
            UpdateExpression="ADD used :one SET expiresAt = if_not_exists(expiresAt, :exp)",
            ConditionExpression="attribute_not_exists(used) OR used < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": DAILY_QUOTA, ":exp": now + 2 * 86400},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    client = headers.get("x-trustlens-client", "")
    if not (8 <= len(client) <= 64):
        return invalid("x-trustlens-client header is required")
    try:
        doc = json.loads(event.get("body") or "")
    except ValueError:
        return invalid("body must be JSON")
    error = validate(doc)
    if error:
        return invalid(error)

    now = int(time.time())
    product_id = str(doc["product"]["product_id"])
    estimated = 60 + 6 * len(doc["reviews"]) // 50  # rough; replaced by Bedrock measurements in README

    # Same client, same product, recent finished result: hand it back without charging (P2: re-analysis cost).
    latest = TABLE.get_item(Key={"recordId": f"latest#{client}#{product_id}"}).get("Item")
    if latest and not doc.get("force") and now - int(latest["createdAt"]) < REUSE_HOURS * 3600:
        job = TABLE.get_item(Key={"recordId": latest["analysisId"]}).get("Item") or {}
        if job.get("status") == "DONE":  # a PARTIAL (rules-only) result is retried, not reused
            return reply(200, {"analysisId": latest["analysisId"], "status": job["status"], "reused": True,
                               "quota_remaining": remaining(client), "poll_after_seconds": 0, "estimated_seconds": 0})

    if not charge(client, now):
        tomorrow = (datetime.datetime.now(KST) + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return reply(429, {"error": "QUOTA_EXCEEDED", "quota_remaining": 0, "daily_quota": DAILY_QUOTA,
                           "resets_at": tomorrow.isoformat()})

    analysis_id = uuid.uuid4().hex
    S3.put_object(Bucket=os.environ["REQUEST_BUCKET"], Key=f"requests/{analysis_id}.json",
                  Body=json.dumps(doc, ensure_ascii=False).encode(), ContentType="application/json")
    expires = now + RETENTION_DAYS * 86400
    TABLE.put_item(Item={"recordId": analysis_id, "status": "PENDING", "clientId": client, "productId": product_id,
                         "productTitle": (doc["product"].get("title") or "")[:200], "quotaKey": quota_key(client),
                         "reviewCount": len(doc["reviews"]), "estimatedSeconds": estimated,
                         "createdAt": now, "expiresAt": expires})
    TABLE.put_item(Item={"recordId": f"latest#{client}#{product_id}", "analysisId": analysis_id,
                         "createdAt": now, "expiresAt": expires})
    LAMBDA.invoke(FunctionName=os.environ["ANALYZE_FUNCTION"], InvocationType="Event",
                  Payload=json.dumps({"analysisId": analysis_id}).encode())
    return reply(202, {"analysisId": analysis_id, "status": "PENDING", "quota_remaining": remaining(client),
                       "poll_after_seconds": 30, "estimated_seconds": estimated})
