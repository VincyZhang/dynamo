# Dynamo CI Structure Report

> 基于 `.github/workflows/` 全部 workflow 文件 + `tests/serve/test_vllm.py` 整理

---

## 一、触发条件总览

| Workflow 文件 | 触发事件 | 触发分支/条件 |
|---|---|---|
| `pre-merge.yml` | push + PR | 所有分支（含 main、release、pull-request/*） |
| `container-validation-dynamo.yml` | push | main、release/*、pull-request/[0-9]+ |
| `pr.yaml` | push | pull-request/[0-9]+（也支持 workflow_dispatch） |
| `post-merge-ci.yml` | push | main、release/*.*.* |
| `nightly-ci.yml` | cron 每天 08:00 UTC | main（也支持 workflow_dispatch） |
| `build-on-demand.yml` | workflow_dispatch 手动 | 任意分支，按需构建 |
| `trigger_ci.yml` | push | main、release/*、pull-request/[0-9]+（触发 GitLab pipeline） |
| `release.yml` | push | release/* 分支 |

---

## 二、Workflow 详细结构

### 1. `pre-merge.yml` — 代码质量检查（所有 PR/push 均触发）

**触发**: 所有 PR 分支（push） + push to main / release  
**Runner**: ubuntu-latest（pre-commit）、prod-builder-amd-v1（rust）

```
pre-commit
  └─ ruff / black / isort 等 lint 检查（3min timeout）

rust-tests  [matrix: 4 dirs × (format / deny / compile / doc / unit)]
  目录:
    - .
    - lib/bindings/python
    - lib/runtime/examples
    - lib/bindings/kvbm
  步骤:
    1. cargo fmt --check
    2. cargo deny check licenses
    3. cargo build --all-features
    4. cargo test --doc
    5. cargo test --lib --bins

rust-clippy  [同 4 目录 matrix]
  步骤:
    1. metadata lock 一致性检查
    2. Cargo.lock 与 Cargo.toml 同步验证
    3. cargo clippy --all-features --all-targets
```

**产出**: 无镜像产物，仅 pass/fail 状态。

---

### 2. `container-validation-dynamo.yml` — Dynamo Runtime 框架无关测试

**触发**: push to main / release/* / pull-request/[0-9]+  
**路径过滤**: 忽略 .md / .rst 纯文档变更  
**Runner**: prod-builder-v3（build）、prod-tester-amd-gpu-v1（test）

```
changed-files
  └─ 检测文件变更（filters.yaml），输出 core/vllm/sglang/trtllm flags

build  (needs: changed-files)
  Runner: prod-builder-v3
  步骤:
    1. container/render.py --target=dynamo --framework=dynamo
       → 生成 Dockerfile
    2. docker-remote-build 构建 runtime 镜像
       → ECR: {sha}-dynamo-cuda{N}-amd64
    3. docker buildx build (Dockerfile.test) 构建 test 镜像
       → ECR: {sha}-dynamo-test-cuda{N}-amd64

rust-checks  (needs: build)
  Runner: prod-tester-amd-gpu-v1
  在 runtime 容器内执行 lib/llm 目录下的 Rust 工具链：
    features: block-manager, media-ffmpeg, testing-nixl, integration
    - cargo fmt --check
    - cargo clippy --all-features
    - cargo test (lib/llm)

test-parallel  (needs: build)
  Runner: prod-builder-amd-v1（无 GPU）
  pytest markers: "pre_merge and parallel and not (vllm or sglang or trtllm) and gpu_0"
  parallel workers: 4（-n auto）
  含 mypy 类型检查: true
  → 运行所有非框架、parallel 标记的 CPU-only 测试

test-sequential  (needs: build)
  Runner: prod-builder-amd-v1
  pytest markers: "pre_merge and not parallel and not (vllm or sglang or trtllm) and gpu_0"
  → 运行所有非框架、non-parallel 的 CPU-only 测试（顺序执行）
```

**产出**: ECR 中的 dynamo runtime + test 镜像（但不复制到 ACR）。

---

### 3. `pr.yaml` — PR 主阻塞流水线

**触发**: push to pull-request/[0-9]+  
**并发**: 同一 PR 的新 push 取消旧 run（cancel-in-progress: true）

```
changed-files
  └─ 检测 core / operator / deploy / vllm / sglang / trtllm 变更
  └─ 输出 BUILDER_NAME（b-{run_id}-{attempt}）

backend-status-check  [阻塞合并的关键 job]
  needs: [changed-files, vllm-pipeline, sglang-pipeline, trtllm-pipeline, xpu-test, operator]
  → jq 校验所有 needs 都是 success 或 skipped

operator  (仅当 operator 文件变更时触发)
  Runner: prod-default-v2
  步骤:
    1. docker buildx lint (arm64 target)
    2. docker buildx test (arm64 target)
    3. make check (uncommitted changes)
    4. docker buildx build --push
       → ECR + ACR: {sha}-operator (amd64 + arm64)

vllm-pipeline  (if: core || vllm || deploy changed)
  → 调用 build-test-distribute-flavor-matrix.yml
  platforms: amd64 + arm64
  cuda_versions: 12.9, 13.0
  markers:
    CPU:       "pre_merge and vllm and gpu_0"
    单卡 GPU:  "pre_merge and vllm and gpu_1"
    多卡 GPU:  禁用（TODO）

sglang-pipeline  (if: core || sglang || deploy changed)
  → 同上，framework=sglang
  markers:
    CPU:       "pre_merge and sglang and gpu_0"
    单卡 GPU:  "pre_merge and sglang and gpu_1"

trtllm-pipeline  (if: core || trtllm || deploy changed)
  → 同上，framework=trtllm，cuda_versions: 13.1 only
  markers:
    CPU:       "pre_merge and trtllm and gpu_0"
    单卡 GPU:  "pre_merge and trtllm and gpu_1"

xpu-test  ★新增（if: core || vllm || deploy changed）
  Runner: prod-tester-xpu-v1
  Timeout: 60 min
  Image: {ACR}/ai-dynamo/dynamo:main-vllm-xpu-amd64
  步骤:
    1. bash -n 语法检查 examples/backends/vllm/launch/*xpu*.sh
    2. pytest -m "pre_merge and vllm and xpu" tests/serve/test_vllm_xpu.py
       (仅文件存在时运行)
    3. 上传 test-results artifact

— 以下 4 个 job 仅 workflow_dispatch 且 run_deploy_operator=true 时运行 —

deploy-operator
  Runner: prod-default-small-v2
  → 确定 operator tag（优先使用本次构建产物，否则 fallback main-operator）
  → 创建 AKS k8s namespace
  → 部署 dynamo operator

deploy-test-vllm  [matrix: agg / agg_router / disagg / disagg_router]
  Runner: prod-default-small-v2
  → 在 AKS 上对每个 profile 运行端到端部署测试（25min timeout）

deploy-test-sglang  [matrix: agg / agg_router]
deploy-test-trtllm  [matrix: agg / agg_router]

deploy-status-check
  → 校验所有 deploy-test 都通过
```

---

### 4. `build-test-distribute-flavor.yml` — 单 framework×platform×cuda 完整 Pipeline（被上述 workflow 调用）

**inputs**: framework / platform / cuda_version / markers / build_only 等  
**这是核心可复用 workflow，被 pr.yaml / post-merge-ci.yml / nightly-ci.yml / build-on-demand.yml 调用。**

```
build  (Runner: prod-builder-v3)
  步骤:
    1. container/render.py --target={target} --framework={framework}
       --cuda-version={cuda_version} [--make-efa]
       → 生成 Dockerfile.{framework}
    2. .github/actions/docker-remote-build
       → 构建 runtime 镜像，推送到 ECR
       tag: {sha}-{framework}[-efa]-cuda{N}-{platform}
    3. docker buildx build (Dockerfile.test)
       → 构建 test 镜像，推送到 ECR
       tag: {sha}-{framework}[-efa]-test-cuda{N}-{platform}
    产出: ECR runtime 镜像 + ECR test 镜像

test  (needs: build，Runner: prod-tester-amd-gpu-v1 / prod-tester-arm-v1)
  步骤:
    1. docker run deploy/sanity_check.py --runtime-check --no-gpu-check
       → 验证 ai-dynamo packages 正确安装
    2. CPU-only 测试（并行）
       pytest marks: {cpu_only_test_markers}（见各 workflow 传参）
       parallel_mode: auto（-n auto）
       含 mypy 类型检查
    3. 单卡 GPU 测试（串行，仅 amd64）
       pytest marks: {single_gpu_test_markers}
       parallel_mode: none
       dind as sidecar（docker-in-docker，用于启动服务进程）

multi-gpu-test  (needs: build，Runner: prod-tester-amd-gpu-4-v1，仅 amd64)
  步骤:
    1. 拉取 test 镜像
    2. pytest marks: {multi_gpu_test_markers}
       parallel_mode: none
       dind as sidecar

copy-to-acr  (needs: [build, test]，Runner: prod-default-small-v2)
  条件: build 成功 AND (test 成功或跳过) AND copy_to_acr=true AND NOT build_only
  步骤:
    1. .github/actions/skopeo-copy
       ECR → ACR: {sha}-{framework}-cuda{N}-{platform}
    产出: ACR 最终镜像（供后续 K8s 部署测试使用）
```

---

### 5. `post-merge-ci.yml` — 合入 main/release 后的完整测试

**触发**: push to main 或 release/*.*.*  
**与 PR 的主要区别**: 运行 `pre_merge OR post_merge` 标记的测试（更多场景）

```
vllm-pipeline / sglang-pipeline / trtllm-pipeline
  平台: amd64 + arm64
  cuda: vllm/sglang=12.9+13.0, trtllm=13.1
  额外镜像 tag（仅 main 分支）:
    ECR+ACR: main-vllm / main-vllm-{sha}
             main-sglang / main-sglang-{sha}
             main-trtllm / main-trtllm-{sha}
  测试 markers:
    CPU:     "(pre_merge or post_merge) and {fw} and gpu_0"    (60min)
    单卡:    "(pre_merge or post_merge) and {fw} and gpu_1"    (60min)
    多卡:    "(pre_merge or post_merge) and {fw} and (gpu_2 or gpu_4)"  (60min)

vllm-efa-pipeline / trtllm-efa-pipeline（build-only，不跑测试，不复制到 ACR）
  用途: 生成 EFA（AWS Elastic Fabric Adapter）支持的镜像
  额外 tag: main-vllm-efa / main-trtllm-efa

operator
  → lint + test （arm64 docker build）
  → make check（uncommitted changes）
  → buildx push (amd64+arm64)
  额外 tag（main 分支）: main-operator

deploy-operator  (needs: operator)
  Runner: prod-default-small-v2
  → 创建 AKS namespace
  → 部署 dynamo operator 到 Azure AKS

deploy-test-vllm   [matrix: agg / agg_router / disagg / disagg_router]
deploy-test-sglang [matrix: agg / agg_router]
deploy-test-trtllm [matrix: agg / agg_router]  (注: disagg/disagg_router 已禁用，超时率100%)
  Runner: prod-default-small-v2，25min timeout
  → 使用 ACR 镜像在 AKS 上部署各框架各 profile，验证端到端推理

deploy-status-check
  → jq 校验所有 deploy-test 通过

clean-k8s-builder
  → always() 清理 buildkit builder
```

**产出镜像（main 分支）**:
- `{ACR}/ai-dynamo/dynamo:main-vllm-cuda12-amd64` 等
- `{ACR}/ai-dynamo/dynamo:main-operator`

---

### 6. `nightly-ci.yml` — 每日定时全量测试

**触发**: 每天 08:00 UTC（北京时间 16:00）+ workflow_dispatch

```
vllm-pipeline / sglang-pipeline / trtllm-pipeline
  平台: amd64 + arm64
  测试 markers:
    CPU:     "nightly and {fw} and gpu_0"
    单卡:    "nightly and {fw} and gpu_1"         (35min timeout for vllm)
    多卡:    "nightly and {fw} and (gpu_2 or gpu_4)"  (120min timeout)
  额外 tag: main 分支覆盖 main-{fw} / main-{fw}-{sha}

notify-slack
  条件: failure() && always()
  → 发送失败通知到 Slack SLACK_NOTIFY_NIGHTLY_WEBHOOK_URL
```

---

### 7. `build-on-demand.yml` — 手动按需构建

**触发**: workflow_dispatch，inputs: build_vllm / build_sglang / build_trtllm / build_operator

```
vllm-pipeline / sglang-pipeline / trtllm-pipeline  (build_only=true，跳过所有测试)
  → 构建并推送 runtime 镜像，打 branch-sanitized tag
  平台: amd64 + arm64
  cuda: vllm/sglang=12.9+13.0, trtllm=13.1

operator  (build_only)
  → buildx push (amd64+arm64) 打 branch tag
```

---

### 8. `trigger_ci.yml` — GitLab CI 桥接

**触发**: push to main / release/* / pull-request/[0-9]+

```
mirror_repo
  Runner: gitlab_ci_runners group
  → .github/workflows/mirror_repo.sh 将仓库镜像同步到 GitLab

trigger-ci
  → 检测 vllm/trtllm/sglang 文件变更
  → 触发 GitLab pipeline（ENABLE_BUILD / ENABLE_PREMERGE / ENABLE_E2E_TEST）
  → 若 GitHub CI 已在运行且 ALLOW_GITLAB_TEST_SKIP=1，则跳过 ENABLE_PREMERGE
```

---

## 三、Pytest Marker 体系

### 阶段 Marker
| Marker | 运行时机 |
|---|---|
| `pre_merge` | PR + Post-merge + Nightly |
| `post_merge` | Post-merge + Nightly（不在 PR 跑） |
| `nightly` | 仅 Nightly CI |

### 资源 Marker
| Marker | 含义 | Runner |
|---|---|---|
| `gpu_0` | CPU-only，无 GPU 需求 | prod-builder-amd-v1 |
| `gpu_1` | 需要 1 块 GPU | prod-tester-amd-gpu-v1 |
| `gpu_2` | 需要 2 块 GPU | prod-tester-amd-gpu-4-v1 |
| `gpu_4` | 需要 4 块 GPU | prod-tester-amd-gpu-4-v1 |

### 框架 Marker
`vllm` / `sglang` / `trtllm` / `xpu` / `lmcache`

### 并发 Marker（仅 container-validation）
`parallel`（-n auto）/ 无 parallel（sequential）

---

## 四、vLLM 测试场景覆盖表（test_vllm.py）

> 按 `pre_merge → post_merge → nightly` 分组

### PR 阶段（pre_merge + vllm + gpu_1 → prod-tester-amd-gpu-v1）

| 测试 config | 脚本 | 模型 | 备注 |
|---|---|---|---|
| `aggregated` | `agg.sh` | Qwen3-0.6B | chat + completion + metrics |
| `aggregated_lmcache` | `agg_lmcache.sh` | Qwen3-0.6B | lmcache 集成；**cuda13 时自动跳过** |
| `aggregated_lmcache_multiproc` | `agg_lmcache_multiproc.sh` | Qwen3-0.6B | 多进程 Prometheus；**cuda13 时自动跳过** |
| `agg-request-plane-tcp` | `agg_request_planes.sh --tcp` | Qwen3-0.6B | TCP request plane |
| `agg-request-plane-http` | `agg_request_planes.sh --http` | Qwen3-0.6B | HTTP request plane |
| `multimodal_disagg_qwen3vl_2b_e_pd` | `disagg_multimodal_e_pd.sh` | Qwen3-VL-2B | 多模态 E-PD 分离，single-gpu |

### Post-Merge 新增（post_merge + vllm + gpu_1）

| 测试 config | 脚本 | 模型 | 备注 |
|---|---|---|---|
| `aggregated_logprobs` | `agg.sh` | Qwen3-0.6B | logprobs 验证 |
| `multimodal_agg_frontend_decoding` | `agg_multimodal.sh` | Qwen2-VL-2B | Rust frontend 图像解码 + NIXL RDMA |
| `multimodal_agg_qwen` | `agg_multimodal.sh` | Qwen2.5-VL-7B | 多模态聚合 7B |

### Post-Merge 新增（post_merge + vllm + gpu_2）

| 测试 config | 脚本 | 模型 | 备注 |
|---|---|---|---|
| `disaggregated` | `disagg.sh` | Qwen3-0.6B | Prefill/Decode 分离 |
| `agg-router` | `agg_router.sh` | Qwen3-0.6B | ⚠️ **skip(DYN-2263) — 永不运行** |
| `agg-router-approx` | `agg_router_approx.sh` | Qwen3-0.6B | ⚠️ **skip(DYN-2264) — 永不运行** |
| `multimodal_disagg_qwen3vl_2b_epd` | `disagg_multimodal_epd.sh` | Qwen3-VL-2B | ⚠️ **skip(DYN-2265) — 永不运行** |

### Nightly（nightly + vllm + gpu_1）

| 测试 config | 脚本 | 模型 | 备注 |
|---|---|---|---|
| `multimodal_agg_llava` | `agg_multimodal.sh` | llava-1.5-7b | xfail(strict=False) |

### Nightly（nightly + vllm + gpu_2）

| 测试 config | 脚本 | 模型 | 备注 |
|---|---|---|---|
| `deepep` | `dsr1_dep.sh` | DeepSeek-V2-Lite | 需要 H100；2-GPU DeepEP |
| `multimodal_video_agg` | `video_agg.sh` | LLaVA-NeXT-Video-7B | 视频多模态 |
| `multimodal_video_disagg` | `video_disagg.sh` | LLaVA-NeXT-Video-7B | 视频 P/D 分离 |

### 从不执行的脚本
| 脚本 | 原因 |
|---|---|
| `agg_flexkv_router.sh` | 无任何 VLLMConfig 引用 |
| `agg_router.sh` | skip(DYN-2263) |
| `agg_router_approx.sh` | skip(DYN-2264) |

---

## 五、镜像命名规则

| 场景 | 镜像 tag 格式 | 存储位置 |
|---|---|---|
| PR 构建产物（临时） | `{sha}-{fw}-cuda{N}-{platform}` | ECR only |
| PR 测试镜像（临时） | `{sha}-{fw}-test-cuda{N}-{platform}` | ECR only |
| PR copy-to-acr（验证通过后） | `{sha}-{fw}-cuda{N}-{platform}` | ACR |
| post-merge main 分支稳定 tag | `main-{fw}` / `main-{fw}-{sha}` | ECR + ACR |
| Operator 镜像 | `{sha}-operator` / `main-operator` | ECR + ACR |
| EFA 镜像 | `{sha}-{fw}-efa-cuda{N}-amd64` | ECR only |
| XPU 测试基础镜像 | `main-vllm-xpu-amd64` | ACR（预构建） |

---

## 六、完整 CI 流程图

```
Push to pull-request/*
│
├─► pre-merge.yml
│     ├─ pre-commit (lint)
│     ├─ rust-tests (4 dirs × 5 steps)
│     └─ rust-clippy (4 dirs × 3 steps)
│
├─► container-validation-dynamo.yml
│     ├─ build → dynamo runtime+test 镜像 → ECR
│     ├─ rust-checks (lib/llm 内 Rust 测试)
│     ├─ test-parallel (pre_merge, non-fw, gpu_0, -n auto)
│     └─ test-sequential (pre_merge, non-fw, gpu_0, sequential)
│
└─► pr.yaml  ← 阻塞合并的主流水线
      ├─ changed-files (检测变更)
      ├─ operator (if operator changed) → ECR+ACR
      ├─ vllm-pipeline (if core/vllm/deploy)
      │     └─► build-test-distribute-flavor × {amd64,arm64} × {cuda12,cuda13}
      │           build → ECR
      │           test  → sanity + cpu(gpu_0) + gpu_1 → ECR test image
      │           copy  → ACR
      ├─ sglang-pipeline (same structure)
      ├─ trtllm-pipeline (same, cuda13.1 only)
      ├─ xpu-test ★ (if core/vllm/deploy, prod-tester-xpu-v1)
      │     └─ bash -n syntax check + pytest pre_merge+vllm+xpu
      └─► backend-status-check (阻塞合并)


Push to main / release/*.*.*
│
└─► post-merge-ci.yml
      ├─ vllm/sglang/trtllm-pipeline
      │     build + test(pre_merge or post_merge) + copy → ECR+ACR
      │     extra: main-{fw} / main-{fw}-efa 稳定 tag
      ├─ vllm-efa / trtllm-efa (build-only)
      ├─ operator → main-operator
      ├─ deploy-operator → AKS k8s
      ├─ deploy-test-vllm (agg/agg_router/disagg/disagg_router)
      ├─ deploy-test-sglang (agg/agg_router)
      ├─ deploy-test-trtllm (agg/agg_router)
      └─ clean-k8s-builder


每天 08:00 UTC
│
└─► nightly-ci.yml
      ├─ vllm/sglang/trtllm-pipeline
      │     build + test(nightly, gpu_0+gpu_1+gpu_2/4)
      └─ notify-slack (on failure)


workflow_dispatch
│
└─► build-on-demand.yml
      └─ {fw}-pipeline (build_only=true，推 branch-tag 镜像)
```

---

## 七、Runner 一览

| Runner 标签 | 用途 | GPU/资源 |
|---|---|---|
| `ubuntu-latest` / `ubuntu-slim` | 轻量工具（lint、git 操作等） | 无 GPU |
| `prod-default-small-v2` | 轻量任务（Copy、Deploy 协调） | 无 GPU |
| `prod-default-v2` | 一般构建任务（Operator） | 无 GPU |
| `prod-builder-v3` | 重型镜像构建（带 buildkit remote builder） | 无 GPU |
| `prod-builder-amd-v1` | 无 GPU 测试（container-validation） | 无 GPU |
| `prod-tester-amd-gpu-v1` | 单卡 GPU 测试 | 1× GPU |
| `prod-tester-amd-gpu-4-v1` | 多卡 GPU 测试 | 4× GPU |
| `prod-tester-xpu-v1` | Intel XPU 测试（新增） | 1× XPU |
| `gitlab_ci_runners` | GitLab 桥接 | — |

---

## 八、Custom Actions 说明（`.github/actions/`）

| Action | 用途 |
|---|---|
| `changed-files` | 基于 filters.yaml 检测变更文件集合 |
| `bootstrap-buildkit` | 初始化/清理 Buildkit remote builder |
| `init-dynamo-builder` | 按 flavor/arch 初始化 Dynamo builder |
| `docker-build` | 标准 docker buildx build wrapper |
| `docker-remote-build` | 调用 remote buildkit builder |
| `docker-login` | 同时登录 ECR 和 ACR |
| `skopeo-copy` | 跨注册表复制镜像（ECR→ACR） |
| `skopeo-login` | skopeo 登录 |
| `pytest` | 在 dind 容器内运行 pytest（支持 parallel_mode / mypy / hf_token） |
| `dynamo-deploy-test` | 在 AKS namespace 内部署并测试 dynamo 场景 |
| `setup-deploy-namespace` | 创建 AKS namespace + 部署 operator |
| `teardown-deploy-namespace` | 清理 AKS namespace |
