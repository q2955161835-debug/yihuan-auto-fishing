from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


def test_manifest_requests_as_invoker_and_per_monitor_v2_dpi():
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

    assert execution_level.attrib["level"] == "asInvoker"
    assert dpi_awareness.text == "PerMonitorV2"


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
    assert "COLLECT(" not in spec
    assert "--uac-admin" not in spec


def test_build_script_gates_packaging_on_tests_and_prints_sha256():
    script = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert ".venv\\Scripts\\python.exe" in script
    assert "-m pytest" in script
    assert "-m PyInstaller" in script
    assert "dist\\异环自动钓鱼.exe" in script
    assert "Get-FileHash -Algorithm SHA256" in script


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
    for relative_path in ("scripts/build.ps1", "try/smoke_exe.ps1"):
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
