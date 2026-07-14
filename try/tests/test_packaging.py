from pathlib import Path
import importlib.util
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_manifest_requests_administrator_and_per_monitor_v2_dpi():
    manifest = ROOT / "packaging" / "app.manifest"

    tree = ET.parse(manifest)
    root = tree.getroot()
    execution_level = next(
        element
        for element in root.iter()
        if element.tag.endswith("requestedExecutionLevel")
    )
    dpi_awareness = next(
        element for element in root.iter() if element.tag.endswith("dpiAwareness")
    )
    dpi_aware = next(
        element for element in root.iter() if element.tag.endswith("dpiAware")
    )

    assert execution_level.attrib["level"] == "requireAdministrator"
    assert execution_level.attrib["uiAccess"] == "false"
    assert dpi_awareness.text == "PerMonitorV2"
    assert dpi_aware.text == "true/pm"


def test_pyinstaller_spec_builds_single_windowed_executable():
    spec = (ROOT / "packaging" / "auto_fishing.spec").read_text(encoding="utf-8")

    assert "collect_all('dxcam')" in spec
    assert "src/auto_fishing/__main__.py" in spec
    assert "Path(SPECPATH).parent" in spec
    assert "pathex=[str(root / 'src')]" in spec
    assert "name='异环自动钓鱼'" in spec
    assert "console=False" in spec
    assert "upx=False" in spec
    assert "app.manifest" in spec
    assert "uac_admin=True" in spec
    assert "COLLECT(" not in spec
    assert "--uac-admin" not in spec


def test_v2_spec_uses_v2_entry_and_filename() -> None:
    spec = (ROOT / "packaging" / "auto_fishing_v2.spec").read_text(
        encoding="utf-8"
    )

    assert "collect_all('dxcam')" in spec
    assert "src/auto_fishing/__main_v2__.py" in spec
    assert "name='异环自动钓鱼V2'" in spec
    assert "console=False" in spec
    assert "uac_admin=True" in spec
    assert "app.manifest" in spec


def test_v2_build_runs_tests_verifies_manifest_and_hash() -> None:
    script = (ROOT / "scripts" / "build_v2.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "auto_fishing_v2.spec" in script
    assert "dist\\异环自动钓鱼V2.exe" in script
    assert "scripts\\verify_release.py" in script
    assert "Get-FileHash -Algorithm SHA256" in script
    assert "-m pytest" in script


def test_build_script_gates_packaging_on_tests_and_prints_sha256():
    script = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert ".venv\\Scripts\\python.exe" in script
    assert "-m pytest" in script
    assert "-m PyInstaller" in script
    assert "dist\\异环自动钓鱼.exe" in script
    assert "scripts\\verify_release.py" in script
    assert "Get-FileHash -Algorithm SHA256" in script


def test_release_verifier_rejects_as_invoker_manifest() -> None:
    verifier_path = ROOT / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("verify_release", verifier_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    as_invoker = b'''<?xml version="1.0" encoding="UTF-8"?>
    <assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
      <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security>
        <requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges>
      </security></trustInfo>
    </assembly>'''

    with pytest.raises(RuntimeError, match="requireAdministrator"):
        module.validate_manifest(as_invoker)


def test_release_verifier_accepts_administrator_manifest() -> None:
    verifier_path = ROOT / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("verify_release_ok", verifier_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    administrator = b'''<?xml version="1.0" encoding="UTF-8"?>
    <assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
      <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security>
        <requestedPrivileges><requestedExecutionLevel level="requireAdministrator" uiAccess="false"/></requestedPrivileges>
      </security></trustInfo>
      <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
        <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
        <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
      </windowsSettings></application>
    </assembly>'''

    module.validate_manifest(administrator)


def test_release_verifier_rejects_manifest_without_legacy_dpi_fallback() -> None:
    verifier_path = ROOT / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("verify_release_dpi", verifier_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing_fallback = b'''<?xml version="1.0" encoding="UTF-8"?>
    <assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
      <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security>
        <requestedPrivileges><requestedExecutionLevel level="requireAdministrator" uiAccess="false"/></requestedPrivileges>
      </security></trustInfo>
      <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
        <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
      </windowsSettings></application>
    </assembly>'''

    with pytest.raises(RuntimeError, match="true/pm"):
        module.validate_manifest(missing_fallback)


def test_build_script_accepts_python_override_and_keeps_project_venv_default():
    script = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8-sig")

    assert "param(" in script
    assert "$PythonPath" in script
    assert "$env:AUTO_FISHING_PYTHON" in script
    assert ".venv\\Scripts\\python.exe" in script


def test_install_documentation_installs_project_before_running_it():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    requirements = agents.index("-m pip install -r requirements-dev.txt")
    editable = agents.index("-m pip install -e .")
    run_module = agents.index("-m auto_fishing")

    assert requirements < editable < run_module


def test_powershell_scripts_are_utf8_bom_for_windows_powershell_compatibility():
    for relative_path in (
        "scripts/build.ps1",
        "scripts/build_v2.ps1",
        "try/smoke_exe.ps1",
    ):
        script_path = ROOT / relative_path
        assert script_path.read_bytes().startswith(b"\xef\xbb\xbf")
        script = script_path.read_text(encoding="utf-8-sig")
        assert "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)" in script


def test_smoke_script_only_observes_and_stops_launcher_process_tree():
    script = (ROOT / "try" / "smoke_exe.ps1").read_text(encoding="utf-8")

    assert "Start-Process" in script
    assert ".HasExited" in script
    assert ".Responding" in script
    assert "finally" in script
    assert "Get-CimInstance Win32_Process" in script
    assert "ParentProcessId" in script
    assert "$OwnedProcessIds" in script
    assert ".Contains([int]$Item.ParentProcessId)" in script
    assert "Get-Process -Id $Item.Id" in script
    assert "CloseMainWindow" in script
    assert "Stop-Process -Id $Remaining.Id" in script
    assert "ExecutablePath" not in script
    assert "Get-ExecutableProcesses" not in script
    assert "发布物仍有残留进程" in script
    assert "SMOKE_OK" in script


def test_smoke_script_polls_for_onefile_child_window_until_startup_deadline():
    script = (ROOT / "try" / "smoke_exe.ps1").read_text(encoding="utf-8-sig")

    assert "$StartupDeadline = [DateTime]::UtcNow.AddSeconds(15)" in script
    assert "$ResponsiveWindows.Count -eq 0 -and" in script
    assert "[DateTime]::UtcNow -lt $StartupDeadline" in script
    assert "Start-Sleep -Seconds 3" not in script


def test_smoke_script_polls_for_process_exit_after_forced_stop():
    script = (ROOT / "try" / "smoke_exe.ps1").read_text(encoding="utf-8-sig")

    stop_index = script.index("Stop-Process -Id $Remaining.Id")
    deadline_index = script.index(
        "$ForceStopDeadline = [DateTime]::UtcNow.AddSeconds(5)"
    )
    poll_index = script.index(
        "[DateTime]::UtcNow -lt $ForceStopDeadline",
        deadline_index,
    )
    residual_index = script.index("发布物仍有残留进程")

    assert stop_index < deadline_index < poll_index < residual_index


def test_smoke_script_accepts_explicit_v2_target_path() -> None:
    script = (ROOT / "try" / "smoke_exe.ps1").read_text(encoding="utf-8-sig")

    assert "param(" in script
    assert "[string]$TargetPath" in script
    assert "$Exe = $TargetPath" in script
    assert "dist\\异环自动钓鱼.exe" in script


def test_smoke_script_requires_elevated_session_before_launch() -> None:
    script = (ROOT / "try" / "smoke_exe.ps1").read_text(encoding="utf-8-sig")

    admin_check = script.index("IsInRole")
    failure = script.index("烟雾测试必须在管理员 PowerShell 中运行")
    launch = script.index("Start-Process -FilePath $ExePath")

    assert admin_check < failure < launch
