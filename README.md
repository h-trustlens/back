# TrustLens SAM 인프라

AWS SAM으로 REST API, Python 3.12 Lambda 5개, DynamoDB를 배포한다.
인프라는 `back/template.yaml`, 배포 기본값은 `back/samconfig.toml`, 자동 배포는 `.github/workflows/deploy.yml`에 정의한다.
스택은 `trustlens`, 리전은 서울(`ap-northeast-2`)이다.

```text
trustlens/
├── .github/workflows/deploy.yml
├── .gitignore · .gitattributes · README.md
└── back/
    ├── lambda/          # Lambda 5개
    ├── template.yaml
    ├── samconfig.toml
    ├── scripts/
    ├── tests/
    └── requirements-ci.txt
```

로컬 SAM 명령은 `cd back` 후 실행한다. `.github/workflows/`는 GitHub가 읽을 수 있도록 저장소 루트에 둔다.

## 최초 설정

1. CloudShell에 `back/scripts/bootstrap-github-aws.sh`를 올리고 `bash ~/bootstrap-github-aws.sh` 실행.
2. 출력된 `AWS_ROLE_ARN`, `AWS_ARTIFACTS_BUCKET`을 `h-trustlens/back`의 Settings → Secrets and variables → Actions → **Variables**에 등록.
3. `git push -u origin main`로 최초 배포. AWS 인증은 OIDC를 사용한다.

이미 master 기준으로 AWS 설정 스크립트를 실행했다면, 최신 스크립트를 CloudShell에 다시 올려 실행한다.
기존 배포 역할의 허용 브랜치를 main으로 갱신하며, 역할 ARN과 버킷 이름은 동일하다.

## 최종 인프라 구성

모든 코드 폴더는 `back/lambda/` 아래에 있으며, 파일명은 `lambda.py`, 진입점은 `lambda_handler`다.

| 코드 파일 | Lambda 이름 | API 연결 |
| --- | --- | --- |
| `submit/lambda.py` | `trustlens-submit` | POST `/v1/reviews/analyze` |
| `fetch/lambda.py` | `trustlens-fetch` | GET `/v1/reviews/analyze/{analysisId}` |
| `preprocess/lambda.py` | `trustlens-preprocess` | 없음 |
| `analyze/lambda.py` | `trustlens-analyze` | 없음 |
| `combine/lambda.py` | `trustlens-combine` | 없음 |

- **공통 권한:** `AWSLambdaBasicExecutionRole`, `AmazonDynamoDBFullAccess`, `AmazonBedrockFullAccess`.
- **DynamoDB:** YAML 리소스 ID는 `trustlensTable`, 실제 테이블 이름은 `trustlens-reviews`, 파티션 키는 `recordId`. 각 Lambda의 `TABLE_NAME`으로 전달.
- **TTL:** `expiresAt` 기준 30일. 저장 코드에서 `int(time.time()) + int(os.environ["RETENTION_DAYS"]) * 86400`을 항목의 `expiresAt`에 숫자로 넣어야 한다. 만료 후 삭제는 비동기로 진행된다.
- **현재 코드:** 501을 반환하는 빈 진입점. 저장·조회·분석 및 Lambda 간 호출은 담당자가 구현한다.

API 이름은 `trustlens-api`, 스테이지는 `api`다. 실제 호출 경로에는 스테이지가 포함된다.

```text
POST https://API_ID.execute-api.ap-northeast-2.amazonaws.com/api/v1/reviews/analyze
GET  https://API_ID.execute-api.ap-northeast-2.amazonaws.com/api/v1/reviews/analyze/{analysisId}
```

`API_ID`는 배포 후 정해지고, `{analysisId}`는 실제 ID로 바꿔 호출한다.

## 자동 배포

**main에 push하거나 PR을 머지했을 때, 아래 경로 중 하나의 변경이 포함된 경우에만 검증 후 자동 재배포한다.**

- `back/lambda/**`: Lambda 코드 폴더 아래 파일.
- `back/template.yaml`: SAM 템플릿. DynamoDB·API Gateway·Lambda 등 어느 설정을 수정해도 대상이다.

main 대상 PR에서는 테스트·SAM 검증·빌드만 수행하고, 실제 배포는 main에 머지된 뒤 실행된다.
README, `back/samconfig.toml`, 스크립트, 테스트, 워크플로 파일 등 **다른 파일만 변경하면 자동 배포하지 않는다.**

Actions의 수동 Run workflow는 경로 필터와 관계없이 실행할 수 있다.

배포 후 Lambda 5개·공통 권한·API 연결·DB 키·TTL을 확인한다.
실제 API 주소는 Actions 실행 Summary와 Artifacts의 `trustlens-outputs`에서 확인한다.
POST 주소 출력키는 `SubmitUrl`, GET 주소 출력키는 `ResultUrl`이다.
