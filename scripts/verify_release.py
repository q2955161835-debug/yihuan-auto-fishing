from __future__ import annotations

import argparse
import ctypes
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path


RT_MANIFEST = 24
APPLICATION_MANIFEST_ID = 1
LOAD_LIBRARY_AS_DATAFILE = 0x00000002


def validate_manifest(manifest: bytes) -> None:
    root = ET.fromstring(manifest.decode("utf-8-sig"))
    execution_level = next(
        (
            element
            for element in root.iter()
            if element.tag.endswith("requestedExecutionLevel")
        ),
        None,
    )
    if execution_level is None:
        raise RuntimeError("release manifest has no requestedExecutionLevel")
    if execution_level.attrib.get("level") != "requireAdministrator":
        raise RuntimeError("release manifest must request requireAdministrator")
    if execution_level.attrib.get("uiAccess") != "false":
        raise RuntimeError("release manifest must keep uiAccess=false")
    dpi_awareness = next(
        (
            element
            for element in root.iter()
            if element.tag.endswith("dpiAwareness")
        ),
        None,
    )
    if dpi_awareness is None or (dpi_awareness.text or "").strip() != "PerMonitorV2":
        raise RuntimeError("release manifest must keep PerMonitorV2")
    dpi_aware = next(
        (
            element
            for element in root.iter()
            if element.tag.endswith("dpiAware")
        ),
        None,
    )
    if dpi_aware is None or (dpi_aware.text or "").strip() != "true/pm":
        raise RuntimeError("release manifest must keep true/pm DPI fallback")


def read_embedded_manifest(executable: Path) -> bytes:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.HANDLE,
        wintypes.DWORD,
    )
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.FindResourceW.argtypes = (
        wintypes.HMODULE,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
    )
    kernel32.FindResourceW.restype = wintypes.HRSRC
    kernel32.LoadResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    kernel32.LoadResource.restype = wintypes.HGLOBAL
    kernel32.SizeofResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    kernel32.SizeofResource.restype = wintypes.DWORD
    kernel32.LockResource.argtypes = (wintypes.HGLOBAL,)
    kernel32.LockResource.restype = wintypes.LPVOID
    kernel32.FreeLibrary.argtypes = (wintypes.HMODULE,)

    module = kernel32.LoadLibraryExW(
        str(executable),
        None,
        LOAD_LIBRARY_AS_DATAFILE,
    )
    if not module:
        raise OSError(ctypes.get_last_error(), "cannot load release executable")

    def integer_resource(identifier: int) -> wintypes.LPCWSTR:
        return ctypes.cast(ctypes.c_void_p(identifier), wintypes.LPCWSTR)

    try:
        resource = kernel32.FindResourceW(
            module,
            integer_resource(APPLICATION_MANIFEST_ID),
            integer_resource(RT_MANIFEST),
        )
        if not resource:
            raise OSError(ctypes.get_last_error(), "release manifest not found")
        loaded = kernel32.LoadResource(module, resource)
        size = int(kernel32.SizeofResource(module, resource))
        address = kernel32.LockResource(loaded)
        if not loaded or size <= 0 or not address:
            raise OSError(ctypes.get_last_error(), "cannot read release manifest")
        return ctypes.string_at(address, size)
    finally:
        kernel32.FreeLibrary(module)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    validate_manifest(read_embedded_manifest(args.executable.resolve()))
    print(
        "RELEASE_MANIFEST_OK requireAdministrator uiAccess=false "
        "dpi=PerMonitorV2 fallback=true/pm"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
