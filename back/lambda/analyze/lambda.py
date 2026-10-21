"""Analysis worker, invoked asynchronously by submit with {"analysisId": ...}.

Runs the v5 pipeline from 06_분석실험 (map -> reduce -> verify -> rule signals and grade). The analysis code
(run.py, grade.py, prompts, schemas) is copied in by scripts/sync_analysis.sh; do not edit those copies here.
Model failure: one retry, then a rules-only PARTIAL result (spec 6.2.1). Other failures: FAILED, quota refunded.
"""
import json
import os
import time

import boto3

import run  # copied from 06_분석실험 by scripts/sync_analysis.sh

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
S3 = boto3.client("s3")
MODEL_ID = os.environ["MODEL_ID"]
PARALLEL = int(os.environ.get("MAP_PARALLEL", "8"))


def refund(job):
    """Give the charged analysis back once (failed or timed-out work should not use up the day's 10)."""
    try:
        TABLE.update_item(Key={"recordId": job["recordId"]}, UpdateExpression="SET refunded = :t",
                          ConditionExpression="attribute_not_exists(refunded)", ExpressionAttributeValues={":t": True})
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return
    TABLE.update_item(Key={"recordId": job["quotaKey"]}, UpdateExpression="ADD used :minus",
                      ExpressionAttributeValues={":minus": -1})


def save(analysis_id, status, **fields):
    names = {"#s": "status"}
    values = {":s": status, ":f": int(time.time())}
    sets = ["#s = :s", "finishedAt = :f"]
    for k, v in fields.items():  # placeholders for every name: some (e.g. RESULT) may be DynamoDB reserved words
        names[f"#{k}"] = k
        sets.append(f"#{k} = :{k}")
        values[f":{k}"] = v
    try:
        TABLE.update_item(Key={"recordId": analysis_id}, UpdateExpression="SET " + ", ".join(sets),
                          ConditionExpression="#s IN (:p, :r)", ExpressionAttributeNames=names,
                          ExpressionAttributeValues={**values, ":p": "PENDING", ":r": "RUNNING"})
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        print(f"{analysis_id}: already TIMEOUT, result discarded")  # fetch gave up and refunded first


def lambda_handler(event, context):
    analysis_id = event["analysisId"]
    job = TABLE.get_item(Key={"recordId": analysis_id}).get("Item")
    if not job or job["status"] != "PENDING":
        return  # duplicate async delivery or already timed out
    TABLE.update_item(Key={"recordId": analysis_id}, UpdateExpression="SET #s = :r, startedAt = :t",
                      ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":r": "RUNNING", ":t": int(time.time())})
    try:
        doc = json.loads(S3.get_object(Bucket=os.environ["REQUEST_BUCKET"], Key=f"requests/{analysis_id}.json")["Body"].read())
    except Exception as e:  # noqa: BLE001 - any read failure ends the job
        save(analysis_id, "FAILED", error=f"INTERNAL_ERROR: request not readable ({type(e).__name__})")
        refund(job)
        return

    started = time.time()
    record, status = None, "DONE"
    for attempt in (1, 2):
        try:
            record = run.pipeline(doc, run.call_bedrock, MODEL_ID, PARALLEL, log=print)
            break
        except Exception as e:  # noqa: BLE001 - model or parsing failure: retry once, then rules only
            print(f"attempt {attempt} failed: {type(e).__name__}: {e}")
            why = f"{type(e).__name__}: {str(e)[:200]}"
    if record is None:
        record, status = run.rules_only(doc, why), "PARTIAL"
    timing = {"seconds": round(time.time() - started, 1), "map_seconds": record.get("map_seconds"),
              "call_seconds": record.get("call_seconds"), "stop_reasons": record.get("stop_reasons"),
              "input_tokens": record.get("input_tokens"), "output_tokens": record.get("output_tokens")}
    save(analysis_id, status, result=json.dumps(record["analysis"], ensure_ascii=False),
         problems=json.dumps(record["verify_problems"][:50], ensure_ascii=False), timing=json.dumps(timing))
    print(json.dumps({"analysisId": analysis_id, "status": status, **timing}))
