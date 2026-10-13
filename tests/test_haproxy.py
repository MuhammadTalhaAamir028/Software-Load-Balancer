"""Tests for the HAProxy template and renderer."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "haproxy" / "haproxy.cfg.tmpl"
RENDER_SCRIPT = ROOT / "haproxy" / "render.sh"

ALGORITHMS = {
    "round_robin": "roundrobin",
    "least_conn": "leastconn",
    "ip_hash": "source",
}


def test_template_matches_load_balancer_settings() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "option forwardfor" in template
    assert "option http-server-close" in template
    assert "retries 0" in template
    assert 'Do not add "option redispatch"' in template
    assert "timeout connect 2s" in template
    assert "timeout server 10s" in template
    assert "timeout http-keep-alive 15s" in template
    assert "timeout check 500ms" in template
    assert "http-check send meth GET uri /health" in template
    assert "http-check expect status 200" in template
    assert "default-server inter 1000 fall 3 rise 2" in template
    assert "stats uri /" in template
    assert "stats refresh 1s" in template


@pytest.mark.parametrize("algorithm,haproxy_algorithm", ALGORITHMS.items())
def test_render_script_maps_project_algorithms(
    algorithm: str, haproxy_algorithm: str
) -> None:
    script = RENDER_SCRIPT.read_text(encoding="utf-8")

    assert re.search(
        rf"^\s*{re.escape(algorithm)}\)\s+haproxy_algo="
        rf'"{re.escape(haproxy_algorithm)}"',
        script,
        re.MULTILINE,
    )


def test_render_script_checks_the_generated_config_with_pinned_image() -> None:
    script = RENDER_SCRIPT.read_text(encoding="utf-8")

    assert "docker run --rm" in script
    assert "haproxy:2.8" in script
    assert 'container_cfg="/usr/local/etc/haproxy/haproxy.cfg"' in script
    assert 'haproxy -c -f "$container_cfg"' in script
    assert "cygpath -m" in script
    assert "MSYS_NO_PATHCONV=1 docker run" in script
    assert "--mount" in script


def test_template_has_all_configured_backends_and_only_algo_placeholder() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    for backend_id, address in (
        ("b1", "10.0.3.11:8000"),
        ("b2", "10.0.3.12:8000"),
        ("b3", "10.0.3.13:8000"),
        ("b4", "10.0.3.14:8000"),
    ):
        assert f"server {backend_id} {address} check" in template

    assert template.count("${ALGO}") == 1
    assert "${" not in template.replace("${ALGO}", "")


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                    reason="renderer integration test requires a POSIX shell")
@pytest.mark.parametrize("algorithm,haproxy_algorithm", ALGORITHMS.items())
def test_renderer_substitutes_algorithm_and_runs_config_check(
    algorithm: str, haproxy_algorithm: str, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    envsubst = bin_dir / "envsubst"
    envsubst.write_text(
        "#!/bin/sh\n"
        'exec "$TEST_PYTHON" -c '
        '\'import os,sys; sys.stdout.write(sys.stdin.read().replace("${ALGO}", os.environ["ALGO"]))\'\n',
        encoding="utf-8",
    )
    envsubst.chmod(0o755)

    docker_args = tmp_path / "docker-args.txt"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$DOCKER_ARGS_FILE"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)

    output_cfg = tmp_path / "rendered.cfg"
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["TEST_PYTHON"] = os.sys.executable
    env["DOCKER_ARGS_FILE"] = str(docker_args)

    result = subprocess.run(
        ["bash", str(RENDER_SCRIPT), algorithm, str(output_cfg)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"balance {haproxy_algorithm}" in output_cfg.read_text(encoding="utf-8")

    args = docker_args.read_text(encoding="utf-8").splitlines()
    assert "haproxy:2.8" in args
    assert args[-4:] == [
        "haproxy",
        "-c",
        "-f",
        "/usr/local/etc/haproxy/haproxy.cfg",
    ]
