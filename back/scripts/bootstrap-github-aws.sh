#!/usr/bin/env bash
# AWS CloudShell에서 한 번 실행한다. AWS 로그인 키를 GitHub에 저장하지 않는다.
set -euo pipefail
export AWS_PAGER=""
REGION="ap-northeast-2"
STACK="trustlens"
ROLE_NAME="${STACK}-github-deploy"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ACCOUNT_ID}-${STACK}-sam-${REGION}"
PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
TASK_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TASK_DIR"' EXIT

# 이미 GitHub OIDC 공급자가 있으면 재사용한다.
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" > "$TASK_DIR/provider.json" 2> "$TASK_DIR/provider.err"; then
  if ! python3 -c 'import json,sys; sys.exit("sts.amazonaws.com" not in json.load(open(sys.argv[1]))["ClientIDList"])' "$TASK_DIR/provider.json"; then
    aws iam add-client-id-to-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" --client-id sts.amazonaws.com
  fi
elif grep -q NoSuchEntity "$TASK_DIR/provider.err"; then
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com > /dev/null
else
  cat "$TASK_DIR/provider.err" >&2
  exit 1
fi

# S3는 빌드한 Lambda ZIP을 담는다. 공개하지 않는다.
if ! aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT_ID" 2> "$TASK_DIR/bucket.err"; then
  if grep -qE '404|Not Found|NoSuchBucket' "$TASK_DIR/bucket.err"; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration "LocationConstraint=$REGION" > /dev/null
  else
    cat "$TASK_DIR/bucket.err" >&2
    exit 1
  fi
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# 이 레포의 main 브랜치만 배포 역할을 사용할 수 있다.
# GitHub의 기존 subject와 ID를 포함하는 새 subject 형식을 둘 다 허용한다.
python3 - "$ACCOUNT_ID" "$REGION" "$BUCKET" "$TASK_DIR" <<'PY'
import json, pathlib, sys
account, region, bucket, directory = sys.argv[1:]
root = pathlib.Path(directory)
trust = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Federated": f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {"StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": [
                "repo:h-trustlens/back:ref:refs/heads/main",
                "repo:h-trustlens@338082873/back@1405589639:ref:refs/heads/main",
            ],
        }},
    }],
}
execution_roles = [
    f"arn:aws:iam::{account}:role/trustlens-LambdaExecutionRole-*",
]
policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "DeployOnlyThisStack", "Effect": "Allow",
            "Action": ["cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:CreateChangeSet",
                       "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet", "cloudformation:DescribeChangeSet",
                       "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents", "cloudformation:GetTemplate",
                       "cloudformation:ListStackResources", "cloudformation:DescribeStackResources"],
            "Resource": [f"arn:aws:cloudformation:{region}:{account}:stack/trustlens/*",
                         f"arn:aws:cloudformation:{region}:aws:transform/Serverless-2016-10-31"],
        },
        {
            "Sid": "ValidateTemplate", "Effect": "Allow",
            "Action": ["cloudformation:GetTemplateSummary", "cloudformation:ValidateTemplate"], "Resource": "*",
        },
        {
            "Sid": "LambdaPackages", "Effect": "Allow",
            "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:GetBucketVersioning"],
            "Resource": f"arn:aws:s3:::{bucket}",
        },
        {
            "Sid": "UploadPackages", "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObject"],
            "Resource": f"arn:aws:s3:::{bucket}/trustlens/*",
        },
        {
            "Sid": "ManageStackFunctions", "Effect": "Allow", "Action": "lambda:*",
            "Resource": f"arn:aws:lambda:{region}:{account}:function:trustlens-*",
        },
        {
            "Sid": "ManageStackTable", "Effect": "Allow", "Action": "dynamodb:*",
            "Resource": [f"arn:aws:dynamodb:{region}:{account}:table/trustlens-reviews",
                         f"arn:aws:dynamodb:{region}:{account}:table/trustlens-reviews/*"],
        },
        {
            "Sid": "ManageRestApi", "Effect": "Allow",
            "Action": ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"],
            "Resource": [f"arn:aws:apigateway:{region}::/restapis*",
                         f"arn:aws:apigateway:{region}::/tags/*"],
        },
        {
            "Sid": "ManageLambdaRoles", "Effect": "Allow",
            "Action": ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
                       "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
                       "iam:ListAttachedRolePolicies", "iam:TagRole", "iam:UntagRole"],
            "Resource": execution_roles,
        },
        {
            "Sid": "AttachRequestedPolicies", "Effect": "Allow",
            "Action": ["iam:AttachRolePolicy", "iam:DetachRolePolicy"], "Resource": execution_roles,
            "Condition": {"ArnEquals": {"iam:PolicyARN": [
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
                "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
            ]}},
        },
        {
            "Sid": "PassRoleOnlyToLambda", "Effect": "Allow", "Action": "iam:PassRole",
            "Resource": execution_roles, "Condition": {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}},
        },
    ],
}
(root / "trust.json").write_text(json.dumps(trust))
(root / "deploy.json").write_text(json.dumps(policy))
PY

if aws iam get-role --role-name "$ROLE_NAME" > /dev/null 2> "$TASK_DIR/role.err"; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$TASK_DIR/trust.json"
elif grep -q NoSuchEntity "$TASK_DIR/role.err"; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$TASK_DIR/trust.json" > /dev/null
else
  cat "$TASK_DIR/role.err" >&2
  exit 1
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name trustlens-deployment --policy-document "file://$TASK_DIR/deploy.json"
printf '\nAWS_ROLE_ARN=arn:aws:iam::%s:role/%s\n' "$ACCOUNT_ID" "$ROLE_NAME"
printf 'AWS_ARTIFACTS_BUCKET=%s\n' "$BUCKET"
printf '\nGitHub Actions에서 Deploy TrustLens 워크플로를 다시 실행하세요.\n'
