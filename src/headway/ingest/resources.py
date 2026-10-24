"""Peak memory of the current process, for the ingest benchmark."""

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Counters(ctypes.Structure):
        """The PROCESS_MEMORY_COUNTERS structure of the Windows API."""

        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    def peak_memory_mb() -> float | None:
        """Return the peak working set of this process in MB, or None if unavailable."""
        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return counters.PeakWorkingSetSize / 1e6 if ok else None

else:
    import resource

    def peak_memory_mb() -> float | None:
        """Return the peak resident memory of this process in MB, or None if unavailable."""
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1e6 if sys.platform == "darwin" else 1e3)  # bytes on macOS, KB elsewhere
