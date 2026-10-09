"""전처리 진입점. API 경로 없이 생성하며 처리 로직은 담당자가 작성한다."""


def lambda_handler(event, context):
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error":"NOT_IMPLEMENTED","function":"preprocess"}',
    }
