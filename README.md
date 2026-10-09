# TrustLens 백엔드

서울 리전(`ap-northeast-2`)의 REST API, Python 3.12 Lambda 5개, DynamoDB 구성.

## API 호출 주소

API Gateway 기본 주소:

```text
https://esul28h1d4.execute-api.ap-northeast-2.amazonaws.com
```

| 메서드 | 연결 Lambda | 호출 URL |
| --- | --- | --- |
| POST | `trustlens-submit` | `https://esul28h1d4.execute-api.ap-northeast-2.amazonaws.com/api/v1/reviews/analyze` |
| GET | `trustlens-fetch` | `https://esul28h1d4.execute-api.ap-northeast-2.amazonaws.com/api/v1/reviews/analyze/{analysisId}` |

POST 요청의 JSON 본문으로 DOM·리뷰 데이터를 전달한다. 요청·응답 JSON 형식은 담당 코드에서 정한다.
GET 요청에서는 `{analysisId}`를 실제 분석 ID로 바꾼다. 예를 들어 ID가 `abc123`이면:

```text
GET https://esul28h1d4.execute-api.ap-northeast-2.amazonaws.com/api/v1/reviews/analyze/abc123
```

레포의 현재 Lambda 코드는 HTTP 501을 반환하는 빈 진입점이다. 저장·조회·분석 및 ID 반환 로직은 담당자가 구현한다.

## Lambda 작업 위치

`back/lambda/` 아래 각 폴더의 `lambda.py`에서 작업하며, 진입점은 `lambda_handler`다.

| 코드 파일 | Lambda 이름 | API 연결 |
| --- | --- | --- |
| `back/lambda/submit/lambda.py` | `trustlens-submit` | POST 접수 |
| `back/lambda/fetch/lambda.py` | `trustlens-fetch` | GET 조회 |
| `back/lambda/preprocess/lambda.py` | `trustlens-preprocess` | 없음 |
| `back/lambda/analyze/lambda.py` | `trustlens-analyze` | 없음 |
| `back/lambda/combine/lambda.py` | `trustlens-combine` | 없음 |

DynamoDB 테이블은 `trustlens-reviews`, 파티션 키는 `recordId`이며, 테이블 이름은 Lambda의 `TABLE_NAME` 환경변수로 전달된다.
TTL은 각 항목의 `expiresAt` 숫자(Unix 초)를 기준으로 동작한다. 저장 코드가 현재 시각에 `RETENTION_DAYS`(30일)를 더해 넣어야 하며, 만료 후 삭제는 비동기로 진행된다.

## 코드 변경과 자동 재배포

`back/lambda/**` 또는 `back/template.yaml` 변경을 포함해 **main에 push하거나 PR을 머지하면 자동 재배포**된다.
main 대상 PR을 올리는 단계에서는 검증만 진행된다. README 등 다른 파일만 변경하면 자동 배포하지 않는다.
