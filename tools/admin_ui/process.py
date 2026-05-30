"""CCIM child process and log management for the admin UI."""

from __future__ import annotations

import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from fastapi import HTTPException

from .config import CCIM_LOG_DIR, ENV_PATH, ROOT
from .settings import effective_env, mask_secret_url, uv_command

_process: subprocess.Popen[str] | None = None
_current_ccim_log_path: Path | None = None
_ccim_job_handle: int | None = None


def write_start_env_snapshot(log_file: Any, env: dict[str, str]) -> None:
    log_file.write("Effective CCIM environment:\n")
    keys = [
        "CCIM_HOST",
        "CCIM_PORT",
        "CCIM_SESSION_PREFIX",
        "CCIM_COMPRESSION_ENABLED",
        "CCIM_COMPRESSION_TRIGGER_TOKENS",
        "CCIM_COMPRESSION_TARGET_TOKENS",
        "CCIM_COMPRESSION_ENABLE_RETRIEVE",
        "CCIM_CURRENT_TURN_COMPRESSION_ENABLED",
        "CCIM_CURRENT_TURN_COMPRESSION_TRIGGER_TOKENS",
        "CCIM_CURRENT_TURN_COMPRESSION_READ_TOOLS",
        "CCIM_COMPRESSION_WRITE_GUARD_ENABLED",
        "CCIM_COMPRESSION_WRITE_GUARD_TOOLS",
        "CCIM_REDIS_URL",
        "CCIM_DATABASE_URL",
        "CCIM_LLM_PROVIDER",
        "CCIM_LLM_MODEL",
    ]
    for key in keys:
        value = env.get(key, "")
        if key in {"CCIM_REDIS_URL", "CCIM_DATABASE_URL"}:
            value = mask_secret_url(value)
        log_file.write(f"  {key}={value}\n")
    log_file.write("\n")


def safe_log_suffix(value: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return suffix.strip("._-")[:40]


def new_ccim_log_path(env: dict[str, str]) -> Path:
    CCIM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = safe_log_suffix(env.get("CCIM_SESSION_PREFIX", ""))
    name = f"ccim_{stamp}"
    if suffix:
        name += f"_{suffix}"
    return CCIM_LOG_DIR / f"{name}.log"


def latest_ccim_log_path() -> Path | None:
    if _current_ccim_log_path is not None and _current_ccim_log_path.exists():
        return _current_ccim_log_path
    if not CCIM_LOG_DIR.exists():
        return None
    logs = sorted(CCIM_LOG_DIR.glob("ccim_*.log"), key=lambda path: path.stat().st_mtime)
    return logs[-1] if logs else None


def create_kill_on_close_job() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
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

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def assign_process_to_job(process: subprocess.Popen[str], job_handle: int | None) -> bool:
    if sys.platform != "win32" or job_handle is None:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    return bool(kernel32.AssignProcessToJobObject(job_handle, process._handle))


def close_job_handle(job_handle: int | None) -> None:
    if sys.platform != "win32" or job_handle is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(job_handle)


def is_running() -> bool:
    return _process is not None and _process.poll() is None


def start_ccim() -> None:
    global _process
    global _current_ccim_log_path
    global _ccim_job_handle
    if is_running():
        return
    env = effective_env()
    port_owner = find_listening_pid(int(env.get("CCIM_PORT", "8080")))
    if port_owner is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"CCIM port is already in use by pid={port_owner}. "
                "Stop that process before starting from admin UI."
            ),
        )
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = new_ccim_log_path(env)
    _current_ccim_log_path = log_path
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write("\n\n=== CCIM start ===\n")
        log_file.write(f"Log file: {log_path}\n")
        write_start_env_snapshot(log_file, env)
        log_file.flush()
        kwargs: dict[str, Any] = {
            "cwd": str(ROOT),
            "env": env,
            "text": True,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        job_handle = create_kill_on_close_job()
        _process = subprocess.Popen([uv_command(), "run", "ccim"], **kwargs)
        if job_handle is not None and assign_process_to_job(_process, job_handle):
            _ccim_job_handle = job_handle
            log_file.write("Windows job guard: enabled (kill child process tree on admin exit)\n")
        else:
            close_job_handle(job_handle)
            _ccim_job_handle = None
            log_file.write("Windows job guard: unavailable; shutdown cleanup only\n")


def stop_ccim() -> None:
    global _process
    global _ccim_job_handle
    if not is_running():
        _process = None
        close_job_handle(_ccim_job_handle)
        _ccim_job_handle = None
        return
    assert _process is not None
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(_process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        _process.send_signal(signal.SIGTERM)
    try:
        _process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _process.kill()
        _process.wait(timeout=5)
    _process = None
    close_job_handle(_ccim_job_handle)
    _ccim_job_handle = None


def find_listening_pid(port: int) -> int | None:
    if sys.platform != "win32":
        return None
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "Get-NetTCPConnection -LocalPort "
                + str(port)
                + " -State Listen -ErrorAction SilentlyContinue "
                + "| Select-Object -First 1 -ExpandProperty OwningProcess"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = proc.stdout.strip()
    if not value:
        return None
    try:
        return int(value.splitlines()[0].strip())
    except ValueError:
        return None


async def status(dependencies: dict[str, Any]) -> dict[str, Any]:
    env = effective_env()
    port_owner = find_listening_pid(int(env.get("CCIM_PORT", "8080")))
    log_path = latest_ccim_log_path()
    return {
        "running": is_running(),
        "pid": _process.pid if is_running() and _process is not None else None,
        "port_owner_pid": port_owner,
        "env_path": str(ENV_PATH),
        "ccim_log_path": str(log_path) if log_path is not None else str(CCIM_LOG_DIR),
        "restart_required_after_save": False,
        "dependencies": dependencies,
    }
