from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = "powershell.exe"


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _run_powershell(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script), *arguments],
        cwd=REPOSITORY_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_compose_services_restart_after_docker_restarts() -> None:
    compose = yaml.safe_load((REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"]["db"]["restart"] == "unless-stopped"
    assert compose["services"]["api"]["restart"] == "unless-stopped"


def test_start_script_waits_for_docker_then_starts_compose_and_checks_health(tmp_path: Path) -> None:
    fake_docker = tmp_path / "fake-docker.cmd"
    calls = tmp_path / "docker-calls.txt"
    first_attempt = tmp_path / "first-attempt"
    second_attempt = tmp_path / "second-attempt"
    log = tmp_path / "startup.log"
    fake_docker.write_text(
        "@echo off\n"
        'if not "%1"=="info" goto compose\n'
        f'echo info>>"{calls}"\n'
        f'if exist "{first_attempt}" goto second\n'
        f'type nul >"{first_attempt}"\n'
        "exit /b 1\n"
        ":second\n"
        f'if exist "{second_attempt}" goto ready\n'
        f'type nul >"{second_attempt}"\n'
        "exit /b 1\n"
        ":ready\n"
        "exit /b 0\n"
        ":compose\n"
        f'echo %*>>"{calls}"\n'
        "exit /b 0\n",
        encoding="utf-8",
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_powershell(
            REPOSITORY_ROOT / "scripts" / "start-nptu-assistant.ps1",
            "-ProjectDirectory",
            str(REPOSITORY_ROOT),
            "-DockerCommand",
            str(fake_docker),
            "-HealthUrl",
            f"http://127.0.0.1:{server.server_port}/health",
            "-DockerWaitTimeoutSeconds",
            "5",
            "-HealthWaitTimeoutSeconds",
            "5",
            "-PollIntervalMilliseconds",
            "25",
            "-LogPath",
            str(log),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    all_calls = calls.read_text(encoding="utf-8").splitlines()
    assert all_calls.count("info") >= 3
    compose_call = all_calls[-1]
    assert compose_call.startswith("compose --project-directory ")
    assert compose_call.endswith(" up -d")
    assert "API 已就緒" in log.read_text(encoding="utf-8-sig")


def test_installer_outputs_safe_at_logon_task_definition() -> None:
    result = _run_powershell(
        REPOSITORY_ROOT / "scripts" / "install-windows-autostart.ps1",
        "-OutputDefinition",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    definition = json.loads(result.stdout)
    assert definition["TaskName"] == "NPTU Campus Assistant Backend"
    assert definition["Execute"].lower().endswith("powershell.exe")
    assert "-WindowStyle Hidden" in definition["Arguments"]
    assert "-File" in definition["Arguments"]
    assert "start-nptu-assistant.ps1" in definition["Arguments"]
    assert definition["Trigger"] == "AtLogOn"
    assert definition["RestartCount"] == 3
    assert definition["RestartIntervalMinutes"] == 1
    assert "OPENAI_API_KEY" not in result.stdout
