# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pytest conftest: shared module-availability checks for GMS tests.

Pytest auto-loads this file before test collection. We add this directory
to sys.path so test files can use `from conftest import HAS_GMS` (absolute).
"""

import importlib
import importlib.util
import os
import sys

# Make this directory importable for absolute imports from test files
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def check_module_available(module_name: str) -> bool:
    """Check if a Python module is available and importable."""
    if importlib.util.find_spec(module_name) is None:
        return False
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


HAS_PYNVML = check_module_available("pynvml")
HAS_GMS = check_module_available("gpu_memory_service")
HAS_TORCH = check_module_available("torch")

# CUDA availability requires torch to be importable first
HAS_CUDA = False
if HAS_TORCH:
    import torch

    HAS_CUDA = torch.cuda.is_available()
