"""Controle stdlib da árvore de processos criada pelo runtime externo."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import weakref
from typing import Any, Dict


_JOBS_LOCK = threading.Lock()
_JOBS: weakref.WeakKeyDictionary[subprocess.Popen, int] = \
    weakref.WeakKeyDictionary()


def popen_group_options() -> Dict[str, Any]:
    """Opções que isolam cada execução em um novo grupo de processos."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _create_windows_job(process: subprocess.Popen) -> int | None:
    """Cria Job Object kill-on-close; retorna ``None`` se indisponível."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

    class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    process_handle = kernel32.OpenProcess(
        0x0001 | 0x0100 | 0x0400, False, process.pid)
    if not process_handle:
        kernel32.CloseHandle(job)
        return None
    try:
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            kernel32.CloseHandle(job)
            return None
    finally:
        kernel32.CloseHandle(process_handle)
    return int(job)


def attach_process_tree(process: subprocess.Popen) -> None:
    """Associa o processo a um Job Object quando o host permite."""
    job = _create_windows_job(process)
    if job is not None:
        with _JOBS_LOCK:
            _JOBS[process] = job


def _pop_job(process: subprocess.Popen) -> int | None:
    with _JOBS_LOCK:
        return _JOBS.pop(process, None)


def _close_windows_job(job: int, terminate: bool) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = wintypes.HANDLE(job)
    if terminate:
        kernel32.TerminateJobObject(handle, 1)
    kernel32.CloseHandle(handle)


def terminate_tree(process: subprocess.Popen) -> None:
    """Termina o processo e todos os descendentes conhecidos."""
    job = _pop_job(process)
    if os.name == "nt":
        # taskkill alcança descendentes criados antes da associação ao Job;
        # o Job cobre os criados depois dela. Usar ambos fecha essa janela.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        if job is not None:
            _close_windows_job(job, terminate=True)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def release_process_tree(process: subprocess.Popen) -> None:
    """Fecha o Job Object e elimina descendentes que sobreviveram ao pai."""
    job = _pop_job(process)
    if os.name == "nt":
        if job is not None:
            _close_windows_job(job, terminate=False)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
