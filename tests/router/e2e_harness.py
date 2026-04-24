# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import time
from typing import Any

from tests.router.common import (
    _test_router_basic,
    _test_router_decisions,
    _test_router_decisions_disagg,
    _test_router_indexers_sync,
)
from tests.router.helper import generate_random_suffix, get_runtime
from tests.utils.constants import DefaultPort
from tests.utils.port_utils import allocate_ports, deallocate_ports
from tests.utils.test_output import resolve_test_output_path

logger = logging.getLogger(__name__)

TEST_PROMPT = (
    "In a quiet meadow tucked between rolling hills, a plump gray rabbit nibbled on "
    "clover beneath the shade of a gnarled oak tree. Its ears twitched at the faint "
    "rustle of leaves, but it remained calm, confident in the safety of its burrow "
    "just a few hops away. The late afternoon sun warmed its fur, and tiny dust "
    "motes danced in the golden light as bees hummed lazily nearby. Though the "
    "rabbit lived a simple life, every day was an adventure of scents, shadows, and "
    "snacks-an endless search for the tastiest patch of greens and the softest spot "
    "to nap."
)


def allocate_frontend_ports(request, count: int) -> list[int]:
    ports = allocate_ports(count, DefaultPort.FRONTEND.value)
    request.addfinalizer(lambda: deallocate_ports(ports))
    return ports


def build_test_payload(model_name: str) -> dict[str, Any]:
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "stream": True,
        "max_tokens": 10,
    }


class ManagedEngineProcessMixin:
    process_name = "worker"
    cleanup_name = "worker resources"
    init_delay_seconds = 5
    init_delay_reason = "initialize before starting next worker"
    cleanup_delay_seconds = 5
    # Max attempts to start a worker before giving up.  Port TOCTOU races
    # (EADDRINUSE) are transient — a retry with freshly allocated ports
    # almost always succeeds.
    max_port_retries = 3

    def __enter__(self):
        logger.info(
            "[%s] Starting %d worker processes sequentially...",
            self.__class__.__name__,
            len(self.worker_processes),
        )

        for i, process in enumerate(self.worker_processes):
            logger.info(
                "[%s] Starting %s %d...", self.__class__.__name__, self.process_name, i
            )
            last_error: Exception | None = None
            for attempt in range(1, self.max_port_retries + 1):
                try:
                    process._logger = logging.getLogger(process.__class__.__name__)
                    process._command_name = process.command[0]
                    process.log_dir = resolve_test_output_path(process.log_dir)
                    os.makedirs(process.log_dir, exist_ok=True)
                    log_name = f"{process._command_name}.log.txt"
                    process._log_path = os.path.join(process.log_dir, log_name)

                    if process.data_dir:
                        process._remove_directory(process.data_dir)

                    process._terminate_all_matching_process_names()
                    logger.info(
                        "[%s] Launching process %d (attempt %d/%d, pid will be assigned)...",
                        self.__class__.__name__,
                        i,
                        attempt,
                        self.max_port_retries,
                    )
                    process._start_process()
                    logger.info(
                        "[%s] Worker %d launched with PID: %s",
                        self.__class__.__name__,
                        i,
                        process.proc.pid if process.proc else "unknown",
                    )
                    time.sleep(process.delayed_start)

                    # Wait for this worker's health check BEFORE launching the next
                    # worker.  On shared-GPU setups (single_gpu=True) workers do
                    # memory profiling during engine init; if two workers profile
                    # concurrently they see each other's allocations and one crashes
                    # with GPU OOM.  By waiting here we guarantee Worker N's engine
                    # init (including memory profiling) completes before Worker N+1
                    # starts.
                    logger.info(
                        "[%s] Checking health for worker %d before launching next...",
                        self.__class__.__name__,
                        i,
                    )
                    elapsed = process._check_ports(process.timeout)
                    process._check_urls(process.timeout - elapsed)
                    process._check_funcs(process.timeout - elapsed)
                    logger.info(
                        "[%s] Worker %d health checks passed",
                        self.__class__.__name__,
                        i,
                    )
                    last_error = None
                    break  # success — exit retry loop

                except Exception as exc:
                    last_error = exc
                    if attempt < self.max_port_retries:
                        logger.warning(
                            "[%s] Worker %d start failed (attempt %d/%d): %s  "
                            "— reallocating ports and retrying...",
                            self.__class__.__name__,
                            i,
                            attempt,
                            self.max_port_retries,
                            exc,
                        )
                        # Tear down the failed attempt
                        try:
                            process.__exit__(None, None, None)
                        except Exception:
                            pass
                        # Ask the owning engine object to reallocate ports for
                        # this worker and rebuild its ManagedProcess.
                        if hasattr(self, "_reallocate_worker_ports"):
                            self._reallocate_worker_ports(i)
                            process = self.worker_processes[i]
                        time.sleep(2)
                    else:
                        logger.exception(
                            "[%s] Failed to start/health-check worker %d "
                            "after %d attempts",
                            self.__class__.__name__,
                            i,
                            self.max_port_retries,
                        )
                        self.__exit__(None, None, None)
                        raise

            if i < len(self.worker_processes) - 1:
                logger.info(
                    "[%s] Waiting %ss for worker %d to %s...",
                    self.__class__.__name__,
                    self.init_delay_seconds,
                    i,
                    self.init_delay_reason,
                )
                time.sleep(self.init_delay_seconds)

        logger.info(
            "[%s] All %d workers started successfully and passed health checks!",
            self.__class__.__name__,
            len(self.worker_processes),
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Collect all ports across all workers BEFORE termination so we can
        # verify they are released afterwards.
        all_ports: list[int] = []
        for process in self.worker_processes:
            all_ports.extend(process.reserved_ports or [])
            all_ports.extend(process.health_check_ports or [])
        # Deduplicate while preserving order
        seen: set[int] = set()
        unique_ports: list[int] = []
        for p in all_ports:
            if p not in seen:
                seen.add(p)
                unique_ports.append(p)

        for i, process in enumerate(self.worker_processes):
            logger.info("Stopping %s %d", self.process_name, i)
            process.__exit__(exc_type, exc_val, exc_tb)

        # Actively verify all ports are released instead of a fixed sleep.
        # Previous tests' Rust runtime (system_status_server) can take
        # several seconds to fully release ports after process termination.
        self._wait_ports_released(unique_ports)

    def _wait_ports_released(
        self, ports: list[int], timeout: float = 15.0, poll_interval: float = 0.5
    ):
        """Poll until none of *ports* are connectable, or *timeout* expires.

        Replaces a fixed ``cleanup_delay_seconds`` sleep with an active check
        that verifies Rust runtime / ZMQ sockets have fully released their
        ports.  This prevents the next test from hitting EADDRINUSE when it
        allocates or binds the same port numbers.
        """
        if not ports:
            time.sleep(self.cleanup_delay_seconds)
            return

        from tests.utils.managed_process import ManagedProcess

        start = time.time()
        remaining = set(ports)
        while remaining and (time.time() - start) < timeout:
            still_held = {
                p for p in remaining if ManagedProcess._is_port_connectable(p)
            }
            if not still_held:
                logger.info(
                    "All %d worker ports verified free in %.1fs",
                    len(ports),
                    time.time() - start,
                )
                break
            remaining = still_held
            logger.info(
                "%d port(s) still held after cleanup: %s — waiting...",
                len(remaining),
                sorted(remaining),
            )
            time.sleep(poll_interval)
        else:
            if remaining:
                logger.warning(
                    "Ports still connectable after %.1fs timeout: %s",
                    timeout,
                    sorted(remaining),
                )

        # Small grace period for kernel socket teardown (TIME_WAIT, etc.)
        time.sleep(1)


def get_engine_endpoint(engine_workers, request_plane: str, component_name: str):
    runtime = get_runtime(request_plane=request_plane)
    return runtime.endpoint(f"{engine_workers.namespace}.{component_name}.generate")


def run_basic_router_test(
    *,
    engine_process_cls,
    engine_args_name: str,
    engine_args: dict[str, Any],
    num_workers: int,
    single_gpu: bool,
    request,
    request_plane: str,
    block_size: int,
    model_name: str,
    frontend_timeout: int = 180,
):
    with engine_process_cls(
        request,
        num_workers=num_workers,
        single_gpu=single_gpu,
        request_plane=request_plane,
        **{engine_args_name: engine_args},
    ) as engine_workers:
        frontend_port = allocate_frontend_ports(request, 1)[0]
        _test_router_basic(
            engine_workers=engine_workers,
            block_size=block_size,
            request=request,
            frontend_port=frontend_port,
            test_payload=build_test_payload(model_name),
            num_requests=10,
            frontend_timeout=frontend_timeout,
            store_backend="etcd",
            request_plane=request_plane,
        )


def run_router_decisions_test(
    *,
    engine_process_cls,
    engine_args_name: str,
    engine_args: dict[str, Any],
    request,
    request_plane: str,
    model_name: str,
    block_size: int,
    component_name: str,
    num_workers: int,
    single_gpu: bool,
    test_dp_rank: bool,
    extra_process_kwargs: dict[str, Any] | None = None,
):
    process_kwargs = extra_process_kwargs or {}
    with engine_process_cls(
        request,
        num_workers=num_workers,
        single_gpu=single_gpu,
        request_plane=request_plane,
        **{engine_args_name: engine_args},
        **process_kwargs,
    ) as engine_workers:
        endpoint = get_engine_endpoint(engine_workers, request_plane, component_name)
        _test_router_decisions(
            engine_workers,
            endpoint,
            model_name,
            request,
            test_dp_rank=test_dp_rank,
            block_size=block_size,
        )


def run_disagg_router_decisions_test(
    *,
    engine_process_cls,
    engine_args_name: str,
    engine_args: dict[str, Any],
    request,
    request_plane: str,
    model_name: str,
    block_size: int,
    num_prefill_workers: int,
    num_decode_workers: int,
    prefill_process_kwargs: dict[str, Any] | None = None,
    decode_process_kwargs: dict[str, Any] | None = None,
):
    shared_namespace = f"test-namespace-{generate_random_suffix()}"
    frontend_port = allocate_frontend_ports(request, 1)[0]

    prefill_kwargs = {
        "namespace": shared_namespace,
        **(prefill_process_kwargs or {}),
    }
    decode_kwargs = {
        "namespace": shared_namespace,
        **(decode_process_kwargs or {}),
    }

    with engine_process_cls(
        request,
        num_workers=num_prefill_workers,
        request_plane=request_plane,
        **{engine_args_name: engine_args},
        **prefill_kwargs,
    ) as prefill_workers:
        with engine_process_cls(
            request,
            num_workers=num_decode_workers,
            request_plane=request_plane,
            **{engine_args_name: engine_args},
            **decode_kwargs,
        ) as decode_workers:
            _test_router_decisions_disagg(
                prefill_workers=prefill_workers,
                decode_workers=decode_workers,
                block_size=block_size,
                request=request,
                frontend_port=frontend_port,
                test_payload=build_test_payload(model_name),
                request_plane=request_plane,
            )


def run_indexers_sync_test(
    *,
    engine_process_cls,
    engine_args_name: str,
    engine_args: dict[str, Any],
    request,
    runtime_services_dynamic_ports,
    store_backend: str,
    durable_kv_events: bool,
    request_plane: str,
    block_size: int,
    model_name: str,
    num_workers: int,
    extra_process_kwargs: dict[str, Any] | None = None,
):
    nats_process, _etcd_process = runtime_services_dynamic_ports
    process_kwargs = extra_process_kwargs or {}

    with engine_process_cls(
        request,
        num_workers=num_workers,
        single_gpu=True,
        request_plane=request_plane,
        store_backend=store_backend,
        durable_kv_events=durable_kv_events,
        **{engine_args_name: engine_args},
        **process_kwargs,
    ) as engine_workers:
        _test_router_indexers_sync(
            engine_workers=engine_workers,
            block_size=block_size,
            model_name=model_name,
            num_workers=num_workers,
            store_backend=store_backend,
            request_plane=request_plane,
            test_nats_interruption=not durable_kv_events,
            nats_server=nats_process if not durable_kv_events else None,
            durable_kv_events=durable_kv_events,
            standalone_indexer_url=getattr(
                engine_workers, "standalone_indexer_url", None
            ),
            standalone_indexer_b_url=getattr(
                engine_workers, "standalone_indexer_b_url", None
            ),
            test_zmq_replay=bool(
                getattr(engine_workers, "standalone_indexer_url", None)
            ),
        )
