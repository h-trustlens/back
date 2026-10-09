"""실제 배포된 역할·함수·REST 연결을 확인한다. 유효한 리뷰나 AI 요청은 보내지 않는다."""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3


def check_http(url, method, allowed, body=None):
    request = Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=20) as result:
            status = result.status
    except HTTPError as error:
        status = error.code
        error.close()
    if status not in allowed:
        raise RuntimeError(f"{method} {url}: 예상 {sorted(allowed)}, 실제 {status}")
    print(f"{method}: HTTP {status} 확인")


def check_ttl(client, table_name):
    ttl = client.describe_time_to_live(TableName=table_name)["TimeToLiveDescription"]
    # TTL 활성화는 최대 한 시간이 걸릴 수 있으므로 진행 중도 허용한다.
    if ttl.get("AttributeName") != "expiresAt" or ttl.get("TimeToLiveStatus") not in {"ENABLING", "ENABLED"}:
        raise RuntimeError(f"DynamoDB TTL 설정 불일치: {ttl}")


def verify(outputs, region):
    values = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    session = boto3.Session(region_name=region)
    functions = session.client("lambda")
    iam = session.client("iam")
    api = session.client("apigateway")
    names = values["FunctionNames"].split(",")
    if len(names) != 5 or len(set(names)) != 5:
        raise RuntimeError("서로 다른 Lambda 다섯 개가 필요합니다.")
    arns, roles = {}, set()
    for name in names:
        config = functions.get_function_configuration(FunctionName=name)
        if (config["State"] != "Active" or config.get("LastUpdateStatus", "Successful") != "Successful"
                or config["Handler"] != "lambda.lambda_handler" or config["Runtime"] != "python3.12"
                or config["Environment"]["Variables"]["TABLE_NAME"] != values["TableName"]
                or config["Environment"]["Variables"].get("RETENTION_DAYS") != "30"):
            raise RuntimeError(f"Lambda 설정 불일치: {name}")
        arns[name] = config["FunctionArn"]
        roles.add(config["Role"].rsplit("/", 1)[-1])
    for role in roles:
        attached = {policy["PolicyName"] for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role)
                    for policy in page["AttachedPolicies"]}
        if not {"AmazonDynamoDBFullAccess", "AmazonBedrockFullAccess", "AWSLambdaBasicExecutionRole"} <= attached:
            raise RuntimeError(f"전체 접근 정책이 누락됐습니다: {role}")
    expected = {
        ("/v1/reviews/analyze", "POST"): names[0],
        ("/v1/reviews/analyze/{analysisId}", "GET"): names[1],
    }
    actual = {}
    for page in api.get_paginator("get_resources").paginate(restApiId=values["RestApiId"]):
        for resource in page["items"]:
            for method in resource.get("resourceMethods", {}):
                integration = api.get_integration(restApiId=values["RestApiId"], resourceId=resource["id"], httpMethod=method)
                actual[(resource["path"], method)] = integration
    if set(actual) != set(expected):
        raise RuntimeError(f"REST 경로 불일치: {list(actual)}")
    for route, name in expected.items():
        integration = actual[route]
        if integration["type"] != "AWS_PROXY" or f"/functions/{arns[name]}/invocations" not in integration["uri"]:
            raise RuntimeError(f"Lambda 연결 불일치: {route}")
    dynamodb = session.client("dynamodb")
    table = dynamodb.describe_table(TableName=values["TableName"])["Table"]
    if table["KeySchema"] != [{"AttributeName": "recordId", "KeyType": "HASH"}]:
        raise RuntimeError("테이블의 recordId 파티션 키가 설정과 다릅니다.")
    check_ttl(dynamodb, values["TableName"])
    # 빈 진입점이면 501, 구현 이후에는 입력 검증 400 / 미존재 ID 404를 기대한다.
    check_http(values["SubmitUrl"], "POST", {400, 501}, b"{}")
    check_http(values["ResultUrl"].replace("{analysisId}", "cicd-check-" + str(uuid4())), "GET", {400, 404, 501})
    print("Lambda 5개, FullAccess 역할, REST 연결 2개, DB 키·TTL·30일 보관 환경변수 확인 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs", required=True)
    parser.add_argument("--region", default="ap-northeast-2")
    args = parser.parse_args()
    verify(json.loads(Path(args.outputs).read_text(encoding="utf-8")), args.region)
