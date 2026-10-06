"""Windows worker-lifetime leases for overlapping Factory mutable workspaces.

The OS handle is the live authority; files are bounded discoverability metadata,
not a second scheduler/queue or source of workflow truth. No TTL reclamation.
Only cooperating Factory workers are fenced; this is not an OS file sandbox.
"""
from __future__ import annotations
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
import json
import os
import uuid


class WorkspaceLeaseUnavailable(RuntimeError):
    pass


def _kernel():
    if os.name != 'nt':
        raise WorkspaceLeaseUnavailable('Factory lifetime lease requires a supported OS backend')
    from ctypes import wintypes as W
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    k.CreateFileW.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
    k.WriteFile.argtypes = [W.HANDLE,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD),ctypes.c_void_p]
    k.WriteFile.restype = W.BOOL
    k.FlushFileBuffers.argtypes = [W.HANDLE]; k.FlushFileBuffers.restype = W.BOOL
    k.ReadFile.argtypes = [W.HANDLE,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD),ctypes.c_void_p]
    k.ReadFile.restype = W.BOOL
    return k


class WorkspaceLease:
    def __init__(self, path: Path, metadata: dict):
        self.path, self.metadata, self._handle = path, metadata, None
        self._k = _kernel()
        # CREATE_NEW + retained DELETE_ON_CLOSE: no path-based unlock or ABA delete.
        h = self._k.CreateFileW(str(path),0x80000000|0x40000000|0x10000,1,None,1,0x80|0x04000000,None)
        if h in (None,ctypes.c_void_p(-1).value):
            raise WorkspaceLeaseUnavailable('Lease is held or unavailable: ' + path.name)
        self._handle = h
        try:
            from ctypes import wintypes as W
            raw = json.dumps(metadata,ensure_ascii=True,separators=(',',':')).encode()
            buf = ctypes.create_string_buffer(raw); written = W.DWORD()
            if not self._k.WriteFile(h,buf,len(raw),ctypes.byref(written),None) or written.value != len(raw):
                raise WorkspaceLeaseUnavailable('Cannot persist lease identity')
            if not self._k.FlushFileBuffers(h):
                raise WorkspaceLeaseUnavailable('Cannot flush lease identity')
        except BaseException:
            self.close(); raise

    def close(self):
        if self._handle is not None:
            h = self._handle
            if not self._k.CloseHandle(h):
                raise WorkspaceLeaseUnavailable('Could not release held lease handle')
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self,*_args):
        self.close()


def _read_metadata(path):
    # A lease owns DELETE access for delete-on-close. Its diagnostic reader must
    # share DELETE as well as READ/WRITE; ordinary Python open() does not.
    from ctypes import wintypes as W
    k=_kernel();h=k.CreateFileW(str(path),0x80000000,7,None,3,0x80,None)
    if h in (None,ctypes.c_void_p(-1).value):
        error=ctypes.get_last_error()
        if error==2:raise FileNotFoundError(str(path))
        raise ctypes.WinError(error)
    try:
        buf=ctypes.create_string_buffer(8193);count=W.DWORD()
        if not k.ReadFile(h,buf,8193,ctypes.byref(count),None):raise ctypes.WinError(ctypes.get_last_error())
        if count.value>8192:raise WorkspaceLeaseUnavailable('Oversized lease metadata')
        return json.loads(buf.raw[:count.value])
    finally:k.CloseHandle(h)


def acquire_workspace_lease(registry: Path, scope: Path, *, task_id: str, run_id: int) -> WorkspaceLease:
    registry, scope = Path(registry), Path(scope)
    if not registry.is_absolute() or not scope.is_absolute():
        raise WorkspaceLeaseUnavailable('Absolute lease registry and mutable scope required')
    if not isinstance(task_id,str) or not task_id or type(run_id) is not int or run_id <= 0:
        raise WorkspaceLeaseUnavailable('Exact task/run identity required')
    # Resolve junctions/case aliases before comparing overlap. Path components,
    # not text prefixes: C:/repo and C:/repo-other are distinct.
    scope = scope.resolve(strict=True)
    registry.mkdir(parents=True,exist_ok=True)
    if registry.resolve() != registry.absolute():
        raise WorkspaceLeaseUnavailable('Lease registry cannot be redirected')
    now = datetime.now(timezone.utc).isoformat()
    identity = {'task_id':task_id,'run_id':run_id,'pid':os.getpid(),'nonce':uuid.uuid4().hex,
                'scope':str(scope),'created_at':now}
    # One very short catalog transaction serializes overlap check AND create.
    # Contention defers the worker; it never spins or reclaims another owner's file.
    with WorkspaceLease(registry/'.catalog.lock', {'pid':os.getpid(),'nonce':uuid.uuid4().hex}):
        for p in registry.glob('*.lease'):
            try:
                data=_read_metadata(p)
                if (not isinstance(data,dict) or not isinstance(data.get('scope'),str)
                        or not Path(data['scope']).is_absolute() or type(data.get('pid')) is not int
                        or data['pid']<=0 or not data.get('nonce')):
                    raise WorkspaceLeaseUnavailable('Unknown lease metadata; no stale-owner guessing')
                held=Path(data['scope']).resolve()
                if scope.is_relative_to(held) or held.is_relative_to(scope):
                    raise WorkspaceLeaseUnavailable('Mutable workspace overlaps an existing live/unknown lease')
            except FileNotFoundError:
                # The held owner closed its handle while we inspected: no claim remains.
                continue
            except (OSError,ValueError,TypeError) as exc:
                raise WorkspaceLeaseUnavailable('Existing lease cannot be verified') from exc
        return WorkspaceLease(registry/(identity['nonce']+'.lease'),identity)
