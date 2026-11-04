"""GET /v1/reviews/analyze/{analysisId}: job status and, when finished, the result (spec table 8 shape).

status: PENDING | RUNNING | DONE | PARTIAL | FAILED | TIMEOUT. The extension stops polling on the last four.
A job still unfinished after TIMEOUT_SECONDS is marked TIMEOUT here and its quota is refunded.
"""
import datetime
import json
import os
import time

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
TIMEOUT_SECONDS = int(os.environ.get("TIMEOUT_SECONDS", "540"))
DAILY_QUOTA = int(os.environ.get("DAILY_QUOTA", "10"))
KST = datetime.timezone(datetime.timedelta(hours=9))


def reply(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, ensure_ascii=False)}


def time_out(job):
    """Mark an overdue job TIMEOUT and refund its quota once."""
    try:
        TABLE.update_item(Key={"recordId": job["recordId"]},
                          UpdateExpression="SET #s = :t, finishedAt = :now, #e = :e, refunded = :true",
                          ConditionExpression="#s IN (:p, :r) AND attribute_not_exists(refunded)",
                          ExpressionAttributeNames={"#s": "status", "#e": "error"},
                          ExpressionAttributeValues={":t": "TIMEOUT", ":now": int(time.time()), ":p": "PENDING", ":r": "RUNNING",
                                                     ":true": True, ":e": "TIMEOUT: analysis did not finish in time"})
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return
    TABLE.update_item(Key={"recordId": job["quotaKey"]}, UpdateExpression="ADD used :minus", ExpressionAttributeValues={":minus": -1})


def lambda_handler(event, context):
    analysis_id = (event.get("pathParameters") or {}).get("analysisId", "")
    if not analysis_id.isalnum():
        return reply(400, {"error": "INVALID_REQUEST", "message": "analysisId is required"})
    job = TABLE.get_item(Key={"recordId": analysis_id}, ConsistentRead=True).get("Item")
    if not job or "status" not in job:
        return reply(404, {"error": "NOT_FOUND", "message": "unknown or expired analysisId"})
    now = int(time.time())
    if job["status"] in ("PENDING", "RUNNING") and now - int(job["createdAt"]) > TIMEOUT_SECONDS:
        time_out(job)
        job = TABLE.get_item(Key={"recordId": analysis_id}, ConsistentRead=True)["Item"]

    quota = TABLE.get_item(Key={"recordId": f"quota#{job['clientId']}#{datetime.datetime.now(KST):%Y%m%d}"}).get("Item") or {}
    body = {"analysisId": analysis_id, "status": job["status"], "product_id": job["productId"],
            "product_title": job.get("productTitle", ""), "review_count": int(job.get("reviewCount", 0)),
            "created_at": int(job["createdAt"]), "elapsed_seconds": now - int(job["createdAt"]),
            "estimated_seconds": int(job.get("estimatedSeconds", 0)),
            "quota_remaining": max(DAILY_QUOTA - int(quota.get("used", 0)), 0)}
    if job.get("result"):
        body["result"] = json.loads(job["result"])
    if job.get("error"):
        body["error"] = job["error"]
    if job.get("timing"):
        body["timing"] = json.loads(job["timing"])
    return reply(200, body)
