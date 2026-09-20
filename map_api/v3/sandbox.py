"""Select local project Python or the optional Docker analysis runtime."""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path
from django.conf import settings

IMAGE = "satellitesense-analysis:local"
MAX_OUTPUT = 128 * 1024 * 1024
MAX_FILES = 512
MAX_PYTHON_TIMEOUT = 600
log = logging.getLogger(__name__)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def docker_executable():
    found = shutil.which("docker")
    if found:
        return found
    installed = Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe")
    return str(installed) if installed.is_file() else "docker"


def python_runtime():
    return getattr(settings, "V3_PYTHON_RUNTIME", "local")


def sandbox_status():
    if python_runtime() == "local":
        from .local_python import local_status
        return local_status()
    if python_runtime() != "docker":
        return {"available": False, "runtime": python_runtime(), "isolated": False,
                "detail": "V3_PYTHON_RUNTIME 必须是 local 或 docker"}
    return _docker_status()


def _docker_status():
    try:
        probe = subprocess.run([docker_executable(), "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                               capture_output=True, text=True, timeout=5)
        return {"available": probe.returncode == 0, "runtime": "docker", "isolated": True, "label": "容器执行", "image": IMAGE,
                "image_id": probe.stdout.strip() if probe.returncode == 0 else None,
                "detail": "ready" if probe.returncode == 0 else "Docker 服务或分析镜像不可用"}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "runtime": "docker", "isolated": True, "label": "容器执行", "image": IMAGE, "detail": "Docker 服务不可用"}


def _bounded_process(command, timeout):
    """Drain streams continuously, retain bounded logs, kill on wall deadline."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    logs = [bytearray(), bytearray()]
    def drain(stream, target):
        for block in iter(lambda: stream.read(8192), b""):
            if len(target) < 64000:
                target.extend(block[:64000 - len(target)])
    readers = [threading.Thread(target=drain, args=(stream, logs[i]), daemon=True)
               for i, stream in enumerate((process.stdout, process.stderr))]
    for reader in readers:
        reader.start()
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError("隔离分析超时，容器已终止")
    finally:
        for reader in readers:
            reader.join(timeout=2)
    return status, [bytes(item).decode("utf-8", errors="replace") for item in logs]


def extract_outputs(stream, output):
    """Validate archive names/types before copying; never follow archive links."""
    total, files, names = 0, [], set()
    output = Path(output).resolve()
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if member.isdir():
                continue
            name = member.name.replace("\\", "/").removeprefix("./")
            path = (output / name).resolve()
            if (not member.isfile() or not name or name in names or Path(name).is_absolute()
                    or not path.is_relative_to(output)):
                raise ValueError("产物包含链接、特殊文件或越界路径")
            total += member.size
            if total > MAX_OUTPUT or len(files) >= MAX_FILES:
                raise ValueError("产物数量或总大小超过限制")
            path.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("产物无法读取")
            with path.open("wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            files.append({"name": name, "size_bytes": path.stat().st_size, "sha256": file_hash(path)})
            names.add(name)
    return files


def _run_root(output_dir):
    """Create one run directory, refusing a pre-existing redirected path."""
    base = (Path(settings.MEDIA_ROOT) / "v3" / "sandbox").resolve()
    root = base / output_dir
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    if not resolved.is_relative_to(base) or resolved != base / output_dir:
        raise ValueError("分析输出路径无效")
    return resolved


def run_python(code, inputs, output_dir, *, timeout=300):
    if not re.fullmatch(r"run-\d+-[a-f0-9]{1,64}", output_dir):
        raise ValueError("分析任务标识无效")
    if not isinstance(code, str) or len(code) > 40000:
        raise ValueError("代码超过限制")
    if type(timeout) is not int or not 1 <= timeout <= MAX_PYTHON_TIMEOUT:
        raise ValueError(f"分析超时必须在 1 到 {MAX_PYTHON_TIMEOUT} 秒之间")
    if python_runtime() == "local":
        from .local_python import run_local_python
        return run_local_python(code, inputs, output_dir, timeout=timeout)
    if python_runtime() != "docker":
        raise ValueError("V3_PYTHON_RUNTIME 必须是 local 或 docker")
    return _run_docker_python(code, inputs, output_dir, timeout=timeout)


def _run_docker_python(code, inputs, output_dir, *, timeout):
    state = sandbox_status()
    if not state["available"]:
        raise RuntimeError("隔离分析容器不可用；不会退回宿主机执行代码")
    root = _run_root(output_dir)
    control = root / "control"
    control.mkdir(exist_ok=True)
    script = control / "analysis.py"
    script.write_text(code, encoding="utf-8")
    output = root / "outputs"
    output.mkdir(exist_ok=True)
    binary, name = docker_executable(), "ss-analysis-" + uuid.uuid4().hex
    command = [binary, "run", "-d", "--name", name, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", "96", "--memory", "2g", "--memory-swap", "2g", "--cpus", "2",
               "--log-driver", "none", "--tmpfs", "/work:rw,nosuid,nodev,size=128m,mode=1777",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
               "--mount", f"type=bind,src={control},dst=/task,readonly", "--workdir", "/work"]
    input_manifest = []
    for index, source in enumerate(inputs):
        source = Path(source).resolve()
        if not source.is_file() or source.is_symlink():
            raise ValueError("输入资产不可用")
        target = f"/inputs/{index:02d}{source.suffix.lower()}"
        command += ["--mount", f"type=bind,src={source},dst={target},readonly"]
        input_manifest.append({"name": target, "sha256": file_hash(source)})
    command += [IMAGE, "sleep", "300"]
    try:
        launch = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if launch.returncode:
            raise RuntimeError("无法启动隔离分析容器")
        launcher = "from pathlib import Path; program=Path('/task/analysis.py'); " + \
            "scope={'__name__':'__main__','__file__':str(program),'inputs':[Path(p) for p in " + \
            repr([item["name"] for item in input_manifest]) + \
            "],'output_dir':Path('/work'),'project_dir':Path('/work')}; " + \
            "exec(compile(program.read_text(encoding='utf-8'),str(program),'exec'),scope)"
        status, logs = _bounded_process([binary, "exec", "--workdir", "/work", name,
                                       "python", "-c", launcher], timeout)
        if status:
            raise RuntimeError("隔离分析失败: " + logs[1][-1000:])
        process = subprocess.Popen([binary, "cp", name + ":/work/.", "-"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        try:
            files = extract_outputs(process.stdout, output)
            if process.wait(timeout=15):
                raise RuntimeError("无法获取沙箱产物")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        return {"runtime": "docker", "isolated": True, "stdout": logs[0], "stderr": logs[1], "outputs": files,
                "relative_output_dir": str(output.relative_to(Path(settings.MEDIA_ROOT) / "v3")).replace("\\", "/"),
                "inputs": input_manifest, "code_sha256": file_hash(script), "image_id": state["image_id"]}
    finally:
        try:
            subprocess.run([binary, "rm", "-f", name], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            # Cleanup failure must not turn a completed result into a host fallback.
            log.warning("Could not confirm cleanup of sandbox container %s", name)
