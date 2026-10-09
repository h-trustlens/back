"""분석 결과 결합 진입점. API 경로 없이 생성한다."""


def lambda_handler(event, context):
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error":"NOT_IMPLEMENTED","function":"combine"}',
    }
