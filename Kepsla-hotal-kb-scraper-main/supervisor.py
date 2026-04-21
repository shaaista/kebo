"""Simple process supervisor for the API server and worker."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from config.settings import settings


def _spawn_process(
    command: list[str],
    *,
    env_updates: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    if env_updates:
        env.update(env_updates)
    return subprocess.Popen(command, env=env)


def main() -> None:
    api_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(settings.app_port),
    ]
    worker_command = [sys.executable, "worker.py"]

    processes = {
        "api": _spawn_process(
            api_command,
            env_updates={"HOTEL_KB_WORKER_AUTO_START_ENABLED": "false"},
        ),
        "worker": _spawn_process(
            worker_command,
            env_updates={"HOTEL_KB_WORKER_AUTO_START_ENABLED": "false"},
        ),
    }

    try:
        while True:
            time.sleep(2)
            for name, process in list(processes.items()):
                if process.poll() is not None:
                    replacement = api_command if name == "api" else worker_command
                    processes[name] = _spawn_process(
                        replacement,
                        env_updates={"HOTEL_KB_WORKER_AUTO_START_ENABLED": "false"},
                    )
    except KeyboardInterrupt:
        for process in processes.values():
            process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
