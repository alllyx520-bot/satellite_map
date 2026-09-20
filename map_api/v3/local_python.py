"""Project Python execution for the explicitly selected local development mode."""
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from django.conf import settings

from .runtime import checkpoint

PACKAGES = ("numpy", "scipy", "rasterio", "pyproj", "shapely", "pandas", "matplotlib")


def configure_matplotlib_fonts():
    """Prefer installed CJK fonts so analysis figures preserve Chinese labels."""
    try:
        import matplotlib
        from matplotlib import font_manager
    except ImportError:
        return None
    preferred = ("Microsoft YaHei", "Microsoft JhengHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC")
    available = {entry.name for entry in font_manager.fontManager.ttflist}
    selected = next((name for name in preferred if name in available), None)
    if selected:
        matplotlib.rcParams["font.sans-serif"] = [selected, "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
    return selected


def local_status():
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"available": Path(sys.executable).is_file(), "runtime": "local", "isolated": False,
            "label": "本地执行", "image": None, "image_id": None, "detail": "使用项目 Python 直接执行分析",
            "python_version": sys.version.split()[0], "packages": versions, "network": True}


def write_runner(control, input_paths, output_path, project_path):
    """Keep submitted source unchanged, including future imports and line numbers."""
    program = control / "analysis.py"
    runner = control / "runner.py"
    runner.write_text(
        "from pathlib import Path\n"
        f"program = Path({str(program)!r})\n"
        f"inputs = [Path(p) for p in {list(map(str, input_paths))!r}]\n"
        f"output_dir = Path({str(output_path)!r})\n"
        f"project_dir = Path({str(project_path)!r})\n"
        "try:\n"
        "    import matplotlib\n"
        "    from matplotlib import font_manager\n"
        "    _preferred_fonts = ('Microsoft YaHei', 'Microsoft JhengHei', 'SimHei', 'Noto Sans CJK SC', 'Source Han Sans SC')\n"
        "    _available_fonts = {entry.name for entry in font_manager.fontManager.ttflist}\n"
        "    matplotlib_font = next((name for name in _preferred_fonts if name in _available_fonts), None)\n"
        "    if matplotlib_font:\n"
        "        matplotlib.rcParams['font.sans-serif'] = [matplotlib_font, 'DejaVu Sans']\n"
        "        matplotlib.rcParams['axes.unicode_minus'] = False\n"
        "except ImportError:\n"
        "    matplotlib_font = None\n"
        "scope = {'__name__': '__main__', '__file__': str(program), 'inputs': inputs, "
        "'output_dir': output_dir, 'project_dir': project_dir, 'matplotlib_font': matplotlib_font}\n"
        "exec(compile(program.read_text(encoding='utf-8'), str(program), 'exec'), scope)\n",
        encoding="utf-8")
    return runner


def _terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=10)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def execute_program(runner, output, timeout):
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", MPLBACKEND="Agg")
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen([sys.executable, "-u", str(runner)], cwd=output, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream, index):
        try:
            for block in iter(lambda: stream.read(8192), b""):
                remaining = 64000 - len(buffers[index])
                buffers[index].extend(block[:remaining])
                truncated[index] |= len(block) > remaining
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for reader in readers:
        reader.start()
    deadline, timed_out = time.monotonic() + timeout, False
    try:
        while process.poll() is None:
            checkpoint()
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_tree(process)
                break
            time.sleep(.05)
    except BaseException:
        _terminate_tree(process)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=2)
    return {"exit_code": process.returncode, "timed_out": timed_out,
            "stdout": buffers[0].decode("utf-8", errors="replace"),
            "stderr": buffers[1].decode("utf-8", errors="replace"),
            "stdout_truncated": truncated[0], "stderr_truncated": truncated[1]}


def collect_outputs(output):
    from .sandbox import MAX_FILES, MAX_OUTPUT, file_hash
    files, size = [], 0
    for directory, dirs, names in os.walk(output, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            size += path.stat().st_size
            if size > MAX_OUTPUT or len(files) >= MAX_FILES:
                raise ValueError("生成的文件已保存在本地；可下载产物的总大小或数量超过限制")
            files.append({"name": path.relative_to(output).as_posix(), "size_bytes": path.stat().st_size,
                          "sha256": file_hash(path)})
    return files


def run_local_python(code, inputs, output_dir, *, timeout):
    from .sandbox import _run_root, file_hash
    checkpoint()
    paths = [Path(item).resolve() for item in inputs]
    if any(not path.is_file() for path in paths):
        raise ValueError("输入影像文件不存在")
    root = _run_root(output_dir)
    control, output = root / "control", root / "outputs"
    control.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    program = control / "analysis.py"
    program.write_text(code, encoding="utf-8")
    runner = write_runner(control, paths, output, settings.BASE_DIR)
    state = local_status()
    result = {"runtime": "local", "isolated": False, "image_id": None,
              "python_version": state["python_version"], "packages": state["packages"],
              "inputs": [{"name": str(path), "sha256": file_hash(path)} for path in paths],
              "code_sha256": file_hash(program), "outputs": [],
              "relative_output_dir": output.relative_to(Path(settings.MEDIA_ROOT).resolve() / "v3").as_posix()}
    started = time.monotonic()
    try:
        result.update(execute_program(runner, output, timeout))
        result["status"] = "completed" if result["exit_code"] == 0 else "failed"
        if result["timed_out"]:
            result["status"] = "timed_out"
            result["error"] = {"code": "python_timeout", "message": f"本地 Python 达到 {timeout} 秒时限，进程已停止"}
        elif result["exit_code"]:
            result["error"] = {"code": "python_error", "message": result["stderr"][-4000:] or f"Python 退出码 {result['exit_code']}"}
        checkpoint()
        result["outputs"] = collect_outputs(output)
        return result
    except BaseException as error:
        result["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) or type(error).__name__ in {"ToolInterrupted", "LostLease"} else "failed"
        result["error"] = {"code": type(error).__name__, "message": str(error)}
        raise
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        (control / "execution.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
