"""Start the unified local workbench and its durable V3 worker together."""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import webbrowser

ROOT = Path(__file__).resolve().parent


def stop(process):
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            process.kill()
        process.wait(timeout=5)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="SatelliteSense 大场景遥感图像智能问答")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    from satellite_map.env import load_project_env
    load_project_env(ROOT)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "satellite_map.settings")
    os.environ.setdefault("DJANGO_DEBUG", "1")
    os.environ.setdefault("PYTHONUTF8", "1")
    if not (ROOT / "static/v3/.vite/manifest.json").is_file():
        raise SystemExit("前端尚未构建。请先在 frontend/ 执行 npm ci 和 npm run build。")
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            raise SystemExit(f"端口 {args.port} 已被使用；请停止旧服务或指定 --port。")
    subprocess.run([sys.executable, "manage.py", "migrate", "--check"], cwd=ROOT, check=True)
    if os.environ.get("DJANGO_DEBUG", "").lower() not in {"1", "true", "yes"}:
        subprocess.run([sys.executable, "manage.py", "collectstatic", "--noinput"], cwd=ROOT, check=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    children = []
    try:
        children.append(subprocess.Popen([sys.executable, "manage.py", "run_v3_worker"], cwd=ROOT, creationflags=flags))
        command = [sys.executable, "manage.py", "runserver", f"127.0.0.1:{args.port}"]
        children.append(subprocess.Popen(command, cwd=ROOT, creationflags=flags))
        print(f"SatelliteSense 正在启动：http://127.0.0.1:{args.port}/；Ctrl+C 停止工作台和 worker。", flush=True)
        opened = args.no_browser
        while all(child.poll() is None for child in children):
            if not opened:
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=.2):
                        webbrowser.open(f"http://127.0.0.1:{args.port}/")
                        opened = True
                except OSError:
                    time.sleep(.2)
            time.sleep(.5)
        raise SystemExit("工作台或 worker 已退出，请查看上方错误。")
    except KeyboardInterrupt:
        print("正在停止，已保存的任务可从检查点恢复。", flush=True)
    finally:
        for child in reversed(children):
            stop(child)


if __name__ == "__main__":
    main()
