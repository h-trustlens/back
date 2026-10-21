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

## 분석 프로토타입 (브랜치 `proto/claude-v5`, 조주환)

`trustlens-proto` 스택(같은 템플릿, 다른 스택 이름)에 배포해 확인했다. 팀 `trustlens` 스택은 건드리지 않았다.

- **POST `/v1/reviews/analyze`**
  - 헤더 `x-trustlens-client`(확장이 만든 설치 ID)와 본문 Review Schema 1.1을 받는다.
  - 응답: `202 {analysisId, status, quota_remaining, poll_after_seconds, estimated_seconds}`
  - 같은 클라이언트가 24시간 안에 같은 상품을 다시 보내면 `200 {reused: true}`로 지난 결과를 돌려주고 횟수를 쓰지 않는다.
  - 오류: `400 INVALID_REQUEST`, `429 QUOTA_EXCEEDED`(하루 10회, KST 0시에 초기화)
- **GET `/v1/reviews/analyze/{analysisId}`**
  - 응답: `{status, result?, quota_remaining, elapsed_seconds, estimated_seconds, timing?}`
  - status는 `PENDING`, `RUNNING`, `DONE`, `PARTIAL`, `FAILED`, `TIMEOUT` 중 하나다.
  - 접수 후 `TIMEOUT_SECONDS`(600초)가 지나면 `TIMEOUT`으로 바꾸고 횟수를 돌려준다.
- **analyze Lambda**
  - `06_분석실험`의 v5 파이프라인(map → reduce → 검증 2건 병렬 → 규칙 신호·등급)을 Bedrock Haiku 4.5(`global.` 교차 리전 프로파일)로 돌린다.
  - 모델 호출이 두 번 실패하면 규칙만으로 만든 `PARTIAL` 결과를 낸다. 그 밖의 실패는 `FAILED`로 끝내고 횟수를 돌려준다.
  - 분석 코드는 `scripts/sync_analysis.sh <06_분석실험 경로>`로 복사해 넣는다. 복사본을 직접 고치지 않는다.
- **리뷰 원문**: S3 `RequestBucket`에 하루만 둔다. DynamoDB에는 상태와 결과만 저장한다.
- **실측(2026-10-21)**: 로컬에서 Bedrock으로 상품 5개를 분석하니 50~90초가 걸렸다(리뷰 175~364건, 호출 7~11번, 입력 16만~19만 토큰). 배포한 Lambda에서는 가습기 46초, 마우스 53초였다. 모두 스펙의 180초 안에 들어간다.
- **확인**
  - `python scripts/e2e.py <API 주소> <리뷰.json>`: 접수부터 완료까지, 그리고 같은 상품 재요청이 횟수를 쓰지 않는지 본다.
  - `scripts/smoke_deploy.py`: 배포 상태를 확인한다.
  - `tests/test_submit_validate.py`: 입력 상한 검증을 확인한다.
