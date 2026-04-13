# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import subprocess
from typing import Any, Optional

try:
    import torch
except ImportError:
    torch = None

logger = logging.getLogger(__name__)


def detect_target_device() -> str:
    """Detect the runtime accelerator expected by the current test environment."""
    if torch is None:
        return "cuda"

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"

    return "cuda"


def get_device_visibility_env_var() -> str:
    """Return the runtime-specific device visibility env var."""
    if detect_target_device() == "xpu":
        return "ZE_AFFINITY_MASK"
    return "CUDA_VISIBLE_DEVICES"


def get_default_vllm_block_size() -> int:
    """Return a runtime-compatible default vLLM block size for tests."""
    return 64 if detect_target_device() == "xpu" else 16


def build_nixl_kv_transfer_config() -> dict[str, Any]:
    """Build a runtime-compatible NIXL kv-transfer config for vLLM tests."""
    config: dict[str, Any] = {
        "kv_connector": "NixlConnector",
        "kv_role": "kv_both",
    }
    if detect_target_device() == "xpu":
        config["kv_buffer_device"] = "xpu"
    return config


def build_nixl_kv_transfer_config_json() -> str:
    """JSON-encode the runtime-compatible NIXL kv-transfer config."""
    return json.dumps(build_nixl_kv_transfer_config())


def get_available_xpu_devices() -> list[int]:
    """Get list of available XPU device indices.

    Returns:
        List of integer device indices (e.g., [0, 1, 2]) in order.
        Empty list if XPU is not available or detection fails.

    Notes:
        - Uses torch.xpu to detect available devices
        - Falls back to [0] if torch is available but device list is empty (single device)
        - Returns [] if torch is not available or XPU is not supported
    """
    if torch is None:
        return []

    if not hasattr(torch, "xpu"):
        return []

    try:
        if not torch.xpu.is_available():
            return []

        device_count = torch.xpu.device_count()
        if device_count <= 0:
            return []

        return list(range(device_count))
    except Exception as e:
        logger.warning(f"Failed to detect XPU devices: {e}")
        return []


def get_xpu_device_free_memory_gib(device_id: int) -> Optional[float]:
    """Get free memory on XPU device in GiB.

    Args:
        device_id: XPU device index (e.g., 0, 1)

    Returns:
        Free memory in GiB, or None if query fails

    Notes:
        - Queries via torch.xpu if available
        - Falls back to None if device is unavailable or query fails
    """
    if torch is None:
        return None

    if not hasattr(torch, "xpu"):
        return None

    try:
        # Try torch.xpu memory query
        if hasattr(torch.xpu, "mem_get_info"):
            free_bytes, total_bytes = torch.xpu.mem_get_info(device_id)
            return free_bytes / (1024**3)  # Convert bytes to GiB
    except Exception as e:
        logger.debug(f"Failed to query XPU device {device_id} via torch.xpu: {e}")

    return None


def select_best_xpu_device() -> int:
    """Auto-select the XPU device with the most available free memory.

    Returns:
        Device index (0, 1, 2, ...) of the device with most free memory.
        Defaults to 0 if detection fails or only 1 device is available.

    Notes:
        - Queries all available devices and returns the one with highest free_memory
        - If multiple devices have equivalent free memory, selects the lowest index
        - Safe fallback to device 0 if any errors occur during detection
        - Logs debug info about device selection for troubleshooting
    """
    available_devices = get_available_xpu_devices()

    if not available_devices:
        logger.debug("No XPU devices detected, defaulting to device 0")
        return 0

    if len(available_devices) == 1:
        logger.debug(f"Single XPU device available: {available_devices[0]}")
        return available_devices[0]

    # Query free memory on each device
    device_memory = {}
    for device_id in available_devices:
        free_gib = get_xpu_device_free_memory_gib(device_id)
        device_memory[device_id] = free_gib
        if free_gib is not None:
            logger.debug(f"XPU device {device_id}: {free_gib:.2f} GiB free")
        else:
            logger.debug(f"XPU device {device_id}: free memory query failed, treating as busy")

    # Select device with most free memory (or lowest index if all queries failed)
    best_device = None
    max_free_memory = -1

    for device_id, free_gib in device_memory.items():
        if free_gib is not None and free_gib > max_free_memory:
            best_device = device_id
            max_free_memory = free_gib

    if best_device is not None:
        logger.debug(f"Selected XPU device {best_device} with {max_free_memory:.2f} GiB free memory")
        return best_device

    # Fallback: all queries failed, return lowest index
    best_device = min(available_devices)
    logger.warning(f"All device memory queries failed; defaulting to device {best_device}")
    return best_device


def get_gpu_memory_utilization(num_workers: int = 1, single_gpu: bool = False) -> float:
    """Get device-aware GPU memory utilization ratio for vLLM tests.

    Args:
        num_workers: Number of vLLM worker processes
        single_gpu: If True, all workers share the same GPU (requires lower utilization)

    Returns:
        GPU memory utilization ratio (0.0-1.0)

    Notes:
        - CUDA (e.g., L40S 48GB): 0.4 per worker is safe
        - XPU (e.g., Intel 23GB): 0.3 per worker max when sharing GPU
        - XPU with single_gpu=True and num_workers>1: reduce to 0.25 for safety margin
    """
    device = detect_target_device()

    if device == "xpu" and single_gpu and num_workers > 1:
        # XPU with multiple workers on same GPU: be conservative
        # 0.25 × 2 workers = 50% total, leaves room for model + overhead
        return 0.25

    if device == "xpu":
        # XPU general case (single worker or multi-GPU)
        return 0.3

    # CUDA (default): more generous utilization
    return 0.45
