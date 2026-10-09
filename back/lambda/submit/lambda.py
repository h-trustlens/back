"""POST 접수 진입점. 저장과 분석 접수 로직은 담당자가 작성한다."""


def lambda_handler(event, context):
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error":"NOT_IMPLEMENTED","function":"submit","message":"Submission logic is not implemented yet."}',
    }
