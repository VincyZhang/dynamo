# CI Runner Prerequisites and Monitoring

This document summarizes:
- Runner machine environment prerequisites
- Monitoring and alerting coverage for runner health and job failures

Scope:
- Repository: `ai-dynamo/dynamo`
- Sources: `.github/workflows/*`, `.github/actions/*`, and workflow helper scripts

## 1. Runner Machine Environment Prerequisites

### 1.1 Core tooling expected on runners

- Docker and Buildx support
  - Jobs and composite actions rely on `docker buildx`, `docker build`, and builder lifecycle commands.
  - Evidence:
    - [`.github/actions/builder-refresher/action.yml`](../.github/actions/builder-refresher/action.yml)
    - [`.github/actions/bootstrap-buildkit/action.yml`](../.github/actions/bootstrap-buildkit/action.yml)
    - [`.github/workflows/post-merge-ci.yml`](../.github/workflows/post-merge-ci.yml)

- Kubernetes tooling and access path
  - CI deploy/test flows use `kubectl`, `vcluster`, and Kubernetes namespaces.
  - Evidence:
    - [`.github/actions/connect-vcluster/action.yml`](../.github/actions/connect-vcluster/action.yml)
    - [`.github/actions/check-vcluster-exists/action.yml`](../.github/actions/check-vcluster-exists/action.yml)
    - [`.github/actions/teardown-dynamo-operator/action.yml`](../.github/actions/teardown-dynamo-operator/action.yml)

- CLI dependencies used by workflows/actions
  - `curl`, `jq`, `skopeo`, and package-manager access (`apt`/`dnf`) for tool install paths.
  - Evidence:
    - [`.github/actions/skopeo-login/action.yml`](../.github/actions/skopeo-login/action.yml)
    - [`.github/workflows/nightly-ci.yml`](../.github/workflows/nightly-ci.yml)
    - [`.github/workflows/post-merge-ci.yml`](../.github/workflows/post-merge-ci.yml)

### 1.2 Credentials and secrets prerequisites

- Cloud/registry secrets are required for image build, pull/push, and deployment.
  - AWS: `AWS_ACCOUNT_ID`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`
  - Azure ACR: `AZURE_ACR_HOSTNAME`, `AZURE_ACR_USER`, `AZURE_ACR_PASSWORD`
  - Kubernetes access: `AZURE_AKS_CI_KUBECONFIG_B64`
  - Model/test credentials: `HF_TOKEN`, Docker Hub credentials in deploy flows
  - Evidence:
    - [`.github/workflows/build-test-distribute-flavor.yml`](../.github/workflows/build-test-distribute-flavor.yml)
    - [`.github/workflows/post-merge-ci.yml`](../.github/workflows/post-merge-ci.yml)
    - [`.github/workflows/pr.yaml`](../.github/workflows/pr.yaml)
    - [`.github/workflows/shared-deploy-test-framework.yml`](../.github/workflows/shared-deploy-test-framework.yml)

### 1.3 Infrastructure and hardware assumptions

- Mixed runner pools are assumed:
  - GitHub-hosted labels: `ubuntu-latest`, `ubuntu-slim`, `ubuntu-24.04`
  - Org/self-hosted labels: `prod-builder-*`, `prod-default-*`, `prod-tester-*`
- Multi-arch and GPU-aware execution is explicitly encoded.
- Evidence:
  - [`.github/workflows/build-test-distribute-flavor.yml`](../.github/workflows/build-test-distribute-flavor.yml)
  - [`.github/workflows/container-validation-dynamo.yml`](../.github/workflows/container-validation-dynamo.yml)
  - [`.github/workflows/post-merge-ci.yml`](../.github/workflows/post-merge-ci.yml)

### 1.4 BuildKit/Kubernetes fallback assumptions

- BuildKit is used in remote mode with Kubernetes fallback.
- Kubernetes fallback requires namespace/resource/toleration settings and sufficient cluster capacity.
- Evidence:
  - [`.github/actions/bootstrap-buildkit/action.yml`](../.github/actions/bootstrap-buildkit/action.yml)
  - [`.github/actions/init-dynamo-builder/action.yml`](../.github/actions/init-dynamo-builder/action.yml)

## 2. Monitoring and Alerting for Runner Health and Job Failures

### 2.1 Job failure alerting

- Nightly and post-merge pipelines include Slack notifications gated by `if: always() && failure()`.
- These jobs query GitHub Actions Jobs API and include failed job names in alert payloads.
- Evidence:
  - [`.github/workflows/nightly-ci.yml`](../.github/workflows/nightly-ci.yml)
  - [`.github/workflows/post-merge-ci.yml`](../.github/workflows/post-merge-ci.yml)

### 2.2 Runner/builder health checks

- BuildKit builder health is explicitly checked with `docker buildx inspect --bootstrap`.
- Unhealthy builders are removed and re-initialized automatically.
- Evidence:
  - [`.github/actions/builder-refresher/action.yml`](../.github/actions/builder-refresher/action.yml)

### 2.3 Degradation warnings and ops signal

- Fallback to Kubernetes BuildKit path emits warning messages and step summary notices to alert ops.
- Evidence:
  - [`.github/actions/bootstrap-buildkit/action.yml`](../.github/actions/bootstrap-buildkit/action.yml)

### 2.4 Metrics collection coverage

- Workflow/job/step metrics upload logic exists via helper script.
- Evidence:
  - [`.github/workflows/upload_complete_workflow_metrics.py`](../.github/workflows/upload_complete_workflow_metrics.py)

## 3. Practical Conclusion

- Prerequisites are clearly defined at workflow/action level and are substantial (Docker/Buildx, Kubernetes tooling, cloud credentials, multi-arch runner pools).
- Job failure alerting is implemented for major scheduled and post-merge pipelines.
- Builder health checks and automated recovery are present for BuildKit.
- There is no explicit evidence in `.github` of host-level runner daemon monitoring (for example, system service telemetry) beyond CI-level checks and alerts.