"""Bedrock 분석 진입점. 모델과 분석 로직은 담당자가 결정한다."""


def lambda_handler(event, context):
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error":"NOT_IMPLEMENTED","function":"analyze"}',
    }
