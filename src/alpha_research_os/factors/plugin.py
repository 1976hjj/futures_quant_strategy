"""Contracts, immutable storage, and isolated execution for Python factor plugins."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from alpha_research_os.kernel.artifacts import ArtifactRef, ArtifactStore
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash
from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import DataDomain, Digest, FrozenSpec, Identifier, ImplementationType, Version

from ._plugin_worker import POLICY_VERSION, PluginSourceError, validate_plugin_source
from .registry import RegisteredFactor
from .runtime import FeatureInputRow, RawFactorValue

RUNTIME_VERSION = "python-plugin-runner-1.0.0"


class PythonPluginSource(FrozenSpec):
    schema_version: Literal["1"] = "1"
    plugin_id: Identifier
    plugin_version: Version
    entrypoint: Literal["factor"] = "factor"
    source: Annotated[str, StringConstraints(min_length=1, max_length=65_536)]

    @field_validator("source")
    @classmethod
    def canonical_source(cls, value: str) -> str:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
        if len(normalized.encode("utf-8")) > 65_536:
            raise ValueError("plugin source exceeds 65536 UTF-8 bytes")
        return normalized

    @property
    def entrypoint_ref(self) -> str:
        return f"{self.plugin_id}:{self.entrypoint}"

    @property
    def implementation_hash(self) -> Digest:
        return content_hash(self)


class PluginSandboxLimits(FrozenSpec):
    timeout_ms: int = Field(default=2_000, ge=100, le=30_000)
    memory_mb: int = Field(default=128, ge=64, le=1_024)
    max_rows: int = Field(default=50_000, ge=1, le=1_000_000)
    max_input_bytes: int = Field(default=64 * 1024 * 1024, ge=1_024, le=512 * 1024 * 1024)
    max_ast_nodes: int = Field(default=512, ge=16, le=10_000)
    max_lookback: int = Field(default=7_560, ge=1, le=7_560)


class PluginRunResult(FrozenSpec):
    runtime_version: Version
    policy_version: Version
    plugin_hash: Digest
    input_hash: Digest
    elapsed_ms: float = Field(ge=0)
    values: tuple[RawFactorValue, ...]


def publish_python_plugin(store: ArtifactStore, plugin: PythonPluginSource) -> ArtifactRef:
    """Persist source by content and bind its logical version once."""

    reference = store.put_bytes(
        plugin.source.encode("utf-8"),
        media_type="text/x-python; profile=alpha-research-os-restricted-factor-v1",
        metadata={
            "entrypoint": plugin.entrypoint_ref,
            "implementation_hash": plugin.implementation_hash,
            "plugin_id": plugin.plugin_id,
            "plugin_version": plugin.plugin_version,
            "sandbox_policy": POLICY_VERSION,
        },
    )
    store.bind_identity("python_plugins", f"{plugin.plugin_id}__{plugin.plugin_version}", reference)
    return reference


def _clean_environment() -> dict[str, str]:
    environment = {"PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1"}
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            if value := os.environ.get(name):
                environment[name] = value
    return environment


class _WindowsJob:
    """Assign the worker to a one-process, memory-capped Windows Job Object."""

    def __init__(self, process: subprocess.Popen[bytes], memory_bytes: int) -> None:
        self._handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00000008 | 0x00000100 | 0x00002000
        information.BasicLimitInformation.ActiveProcessLimit = 1
        information.ProcessMemoryLimit = memory_bytes
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        self._handle = handle
        self._kernel32 = kernel32

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _posix_limits(memory_bytes: int, timeout_ms: int):
    if os.name == "nt":
        return None

    def apply() -> None:
        import resource

        cpu_seconds = max(1, (timeout_ms + 999) // 1000)
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))

    return apply


class PythonPluginRuntime:
    """Run a registered plugin against feature-only rows in an isolated process."""

    def __init__(
        self,
        field_domains: Mapping[str, DataDomain],
        *,
        limits: PluginSandboxLimits | None = None,
    ) -> None:
        self._field_domains = dict(field_domains)
        self._limits = limits or PluginSandboxLimits()
        forbidden = {
            field: domain.value
            for field, domain in self._field_domains.items()
            if domain in {DataDomain.LABEL, DataDomain.HOLDOUT}
        }
        if forbidden:
            raise IntegrityViolation(
                "PLUGIN_PRIVILEGED_DOMAIN",
                "Python plugin runtime cannot expose Label or Holdout fields",
                rule_id="RULE-005",
                context={"fields": forbidden},
            )

    def run(
        self,
        factor: RegisteredFactor,
        plugin: PythonPluginSource,
        rows: Iterable[FeatureInputRow],
    ) -> PluginRunResult:
        if factor.spec.implementation_type is not ImplementationType.PYTHON:
            raise IntegrityViolation(
                "PLUGIN_IMPLEMENTATION_TYPE",
                "PythonPluginRuntime accepts only registered Python factors",
                rule_id="RULE-030",
            )
        if factor.spec.python_entrypoint != plugin.entrypoint_ref:
            raise IntegrityViolation(
                "PLUGIN_ENTRYPOINT_MISMATCH",
                "FactorSpec entrypoint does not identify the supplied plugin",
                rule_id="RULE-027",
            )
        if factor.spec.implementation_hash != plugin.implementation_hash:
            raise IntegrityViolation(
                "PLUGIN_IMPLEMENTATION_HASH_MISMATCH",
                "FactorSpec implementation hash does not match immutable plugin source",
                rule_id="RULE-027",
            )
        declared_fields = tuple(sorted(factor.spec.required_fields))
        missing = sorted(set(declared_fields) - self._field_domains.keys())
        runtime_domains = {self._field_domains[field] for field in declared_fields if field in self._field_domains}
        if missing or runtime_domains != set(factor.spec.data_domains):
            raise IntegrityViolation(
                "PLUGIN_FEATURE_VIEW_CONTRACT_MISMATCH",
                "runtime fields do not satisfy the Python FactorSpec",
                rule_id="RULE-005",
                context={"missing": missing},
            )
        if factor.spec.lookback_sessions > self._limits.max_lookback:
            raise IntegrityViolation(
                "PLUGIN_LOOKBACK_LIMIT",
                "plugin lookback exceeds the sandbox policy",
                rule_id="RULE-030",
            )
        try:
            validate_plugin_source(
                plugin.source,
                declared_fields=declared_fields,
                max_ast_nodes=self._limits.max_ast_nodes,
                max_lookback=factor.spec.lookback_sessions,
            )
        except PluginSourceError as error:
            raise IntegrityViolation(
                "PLUGIN_SOURCE_REJECTED",
                str(error),
                rule_id="RULE-030",
            ) from None

        validated_rows = tuple(FeatureInputRow.model_validate(row) for row in rows)
        if len(validated_rows) > self._limits.max_rows:
            raise IntegrityViolation(
                "PLUGIN_ROW_BUDGET_EXCEEDED",
                "plugin input exceeds the row budget",
                rule_id="RULE-030",
            )
        keys = [(row.instrument_id, row.session) for row in validated_rows]
        if len(keys) != len(set(keys)):
            raise ValueError("plugin input contains duplicate instrument/session rows")
        serialized_rows = []
        for row in validated_rows:
            value_map = row.value_map()
            serialized_rows.append(
                {
                    "available_at": row.available_at.isoformat(),
                    "instrument_id": row.instrument_id,
                    "session": row.session.isoformat(),
                    "values": {field: value_map.get(field) for field in declared_fields},
                }
            )
        input_manifest = {
            "declared_fields": declared_fields,
            "factor_id": factor.spec.factor_id,
            "factor_version": factor.spec.factor_version,
            "implementation_hash": factor.spec.implementation_hash,
            "rows": serialized_rows,
        }
        payload = canonical_json_bytes(
            {
                "declared_fields": declared_fields,
                "max_ast_nodes": self._limits.max_ast_nodes,
                "max_lookback": factor.spec.lookback_sessions,
                "rows": serialized_rows,
                "source": plugin.source,
            }
        )
        if len(payload) > self._limits.max_input_bytes:
            raise IntegrityViolation(
                "PLUGIN_INPUT_BUDGET_EXCEEDED",
                "serialized plugin input exceeds the byte budget",
                rule_id="RULE-030",
            )
        worker = Path(__file__).with_name("_plugin_worker.py").resolve()
        started = time.perf_counter()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryDirectory(prefix="alpha-plugin-") as working_directory:
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-u", str(worker)],
                cwd=working_directory,
                env=_clean_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                preexec_fn=_posix_limits(self._limits.memory_mb * 1024 * 1024, self._limits.timeout_ms),
            )
            job: _WindowsJob | None = None
            try:
                job = _WindowsJob(process, self._limits.memory_mb * 1024 * 1024)
                stdout, stderr = process.communicate(payload, timeout=self._limits.timeout_ms / 1000)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise IntegrityViolation(
                    "PLUGIN_TIMEOUT",
                    "Python factor exceeded its wall-clock budget",
                    rule_id="RULE-030",
                ) from None
            except Exception:
                process.kill()
                process.communicate()
                raise
            finally:
                if job is not None:
                    job.close()
        elapsed_ms = (time.perf_counter() - started) * 1000
        if len(stdout) > self._limits.max_input_bytes:
            raise IntegrityViolation(
                "PLUGIN_OUTPUT_BUDGET_EXCEEDED",
                "plugin worker output exceeds the byte budget",
                rule_id="RULE-030",
            )
        try:
            document = json.loads(stdout)
        except json.JSONDecodeError:
            detail = stderr.decode("utf-8", errors="replace")[:1000]
            raise IntegrityViolation(
                "PLUGIN_WORKER_PROTOCOL",
                f"plugin worker returned invalid output: {detail}",
                rule_id="RULE-030",
            ) from None
        if process.returncode != 0 or not document.get("ok"):
            error = document.get("error", {})
            raise IntegrityViolation(
                "PLUGIN_EXECUTION_FAILED",
                f"{error.get('type', 'WorkerError')}: {error.get('message', 'unknown worker failure')}",
                rule_id="RULE-030",
            )
        if document.get("policy_version") != POLICY_VERSION:
            raise IntegrityViolation(
                "PLUGIN_POLICY_MISMATCH",
                "worker sandbox policy differs from the parent runtime",
                rule_id="RULE-030",
            )
        isolation = document.get("isolation", {})
        allowed_environment = {key.casefold() for key in _clean_environment()}
        actual_environment = {str(key).casefold() for key in isolation.get("environment_keys", ())}
        if (
            not isolation.get("isolated_flag")
            or not isolation.get("no_site_flag")
            or not isolation.get("working_directory_empty")
            or not actual_environment.issubset(allowed_environment)
        ):
            raise IntegrityViolation(
                "PLUGIN_PROCESS_ISOLATION_FAILED",
                "worker process did not satisfy isolated flags, environment, or working-directory policy",
                rule_id="RULE-030",
            )
        expected_keys = {(row.session.isoformat(), row.instrument_id) for row in validated_rows}
        actual_keys = {(row["session"], row["instrument_id"]) for row in document["rows"]}
        if actual_keys != expected_keys or len(document["rows"]) != len(validated_rows):
            raise IntegrityViolation(
                "PLUGIN_OUTPUT_SHAPE_MISMATCH",
                "plugin worker output keys differ from its feature input",
                rule_id="RULE-030",
            )
        values = tuple(
            sorted(
                (
                    RawFactorValue(
                        session=row["session"],
                        instrument_id=row["instrument_id"],
                        factor_id=factor.spec.factor_id,
                        factor_version=factor.spec.factor_version,
                        value=row["value"],
                        available_at=datetime.fromisoformat(row["available_at"]),
                        implementation_hash=factor.spec.implementation_hash,
                    )
                    for row in document["rows"]
                ),
                key=lambda item: (item.session, item.instrument_id),
            )
        )
        return PluginRunResult(
            runtime_version=RUNTIME_VERSION,
            policy_version=POLICY_VERSION,
            plugin_hash=plugin.implementation_hash,
            input_hash=content_hash(input_manifest),
            elapsed_ms=elapsed_ms,
            values=values,
        )
