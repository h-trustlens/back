"""End-to-end check against a deployed stack: submit one Review Schema file, poll until it finishes.

Usage: python scripts/e2e.py <api base, e.g. https://xxx.execute-api.ap-northeast-2.amazonaws.com/api> <review.json> [out.json]
Spends one analysis from today's quota of the e2e client ID (refunded if the analysis fails).
"""
import json
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def call(url, body=None, client=None):
    headers = {"Content-Type": "application/json"}
    if client:
        headers["x-trustlens-client"] = client
    req = Request(url, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                  method="POST" if body is not None else "GET", headers=headers)
    try:
        with urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read())
    except HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main(base, path, out=None):
    doc = json.load(open(path, encoding="utf-8"))
    client = "e2e" + uuid.uuid4().hex[:20]
    started = time.time()
    status, body = call(f"{base}/v1/reviews/analyze", doc, client)
    print("submit", status, {k: v for k, v in body.items() if k != "result"})
    if status not in (200, 202):
        sys.exit(1)
    while True:
        time.sleep(10)
        status, res = call(f"{base}/v1/reviews/analyze/{body['analysisId']}")
        print(f"{time.time() - started:6.1f}s", res.get("status"), res.get("elapsed_seconds"))
        if res.get("status") in ("DONE", "PARTIAL", "FAILED", "TIMEOUT"):
            break
    print("timing", json.dumps(res.get("timing"), ensure_ascii=False))
    if out:
        json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    rel = (res.get("result") or {}).get("reliability", {})
    print("level", rel.get("level"), "| reason", rel.get("reason"))
    status2, again = call(f"{base}/v1/reviews/analyze", doc, client)
    print("resubmit same product:", status2, "reused" if again.get("reused") else "charged", "quota", again.get("quota_remaining"))
    sys.exit(0 if res.get("status") == "DONE" else 1)


if __name__ == "__main__":
    main(*sys.argv[1:])
