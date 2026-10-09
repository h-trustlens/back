"""GET 결과 조회 진입점. DynamoDB 조회 로직은 담당자가 작성한다."""


def lambda_handler(event, context):
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error":"NOT_IMPLEMENTED","function":"fetch"}',
    }
