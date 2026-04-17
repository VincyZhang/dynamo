# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Skip all gpu_memory_service tests on XPU — the package is not available.
collect_ignore_glob = []

try:
    import torch

    if hasattr(torch, "xpu") and torch.xpu.is_available():
        collect_ignore_glob = ["test_*.py"]
except ImportError:
    pass
