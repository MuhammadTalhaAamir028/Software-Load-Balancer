"""Tests for the HAProxy template and renderer.

Run from the repository root:  python -m unittest tests.test_haproxy -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "haproxy" / "haproxy.cfg.tmpl"
RENDER_SCRIPT = ROOT / "haproxy" / "render.sh"

ALGORITHMS = {
    "round_robin": "roundrobin",
    "least_conn": "leastconn",
    "ip_hash": "source",
}


class TestTemplateAndScript(unittest.TestCase):
    def test_template_matches_load_balancer_settings(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")

        for expected in (
            "option forwardfor",
            "option http-server-close",
            "retries 0",
            'Do not add "option redispatch"',
            "timeout connect 2s",
            "timeout server 10s",
            "timeout http-keep-alive 15s",
            "timeout check 500ms",
            "http-check send meth GET uri /health",
            "http-check expect status 200",
            "default-server inter 1000 fall 3 rise 2",
            "stats uri /",
            "stats refresh 1s",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, template)

    def test_render_script_maps_project_algorithms(self) -> None:
        script = RENDER_SCRIPT.read_text(encoding="utf-8")

        for algorithm, haproxy_algorithm in ALGORITHMS.items():
            with self.subTest(algorithm=algorithm):
                self.assertRegex(
                    script,
                    re.compile(
                        rf"^\s*{re.escape(algorithm)}\)\s+haproxy_algo="
                        rf'"{re.escape(haproxy_algorithm)}"',
                        re.MULTILINE,
                    ),
                )

    def test_render_script_checks_the_generated_config_with_pinned_image(self) -> None:
        script = RENDER_SCRIPT.read_text(encoding="utf-8")

        for expected in (
            "docker run --rm",
            "haproxy:2.8",
            'container_cfg="/usr/local/etc/haproxy/haproxy.cfg"',
            'haproxy -c -f "$container_cfg"',
            'chmod 644 "$tmp_cfg"',
            "cygpath -m",
            "MSYS_NO_PATHCONV=1 docker run",
            "--mount",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, script)

    def test_template_has_all_configured_backends_and_only_algo_placeholder(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")

        for backend_id, address in (
            ("b1", "10.0.3.11:8000"),
            ("b2", "10.0.3.12:8000"),
            ("b3", "10.0.3.13:8000"),
            ("b4", "10.0.3.14:8000"),
        ):
            with self.subTest(backend=backend_id):
                self.assertIn(f"server {backend_id} {address} check", template)

        self.assertEqual(template.count("${ALGO}"), 1)
        self.assertNotIn("${", template.replace("${ALGO}", ""))


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None,
                 "renderer integration test requires a POSIX shell")
class TestRenderer(unittest.TestCase):
    def render(self, algorithm: str, tmp: Path):
        """Run render.sh with fake envsubst and docker; return (result, output cfg, docker args file)."""
        bin_dir = tmp / "bin"
        bin_dir.mkdir()

        envsubst = bin_dir / "envsubst"
        envsubst.write_text(
            "#!/bin/sh\n"
            'exec "$TEST_PYTHON" -c '
            '\'import os,sys; sys.stdout.write(sys.stdin.read().replace("${ALGO}", os.environ["ALGO"]))\'\n',
            encoding="utf-8",
        )
        envsubst.chmod(0o755)

        docker_args = tmp / "docker-args.txt"
        docker = bin_dir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" > "$DOCKER_ARGS_FILE"\n',
            encoding="utf-8",
        )
        docker.chmod(0o755)

        output_cfg = tmp / "rendered.cfg"
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["TEST_PYTHON"] = sys.executable
        env["DOCKER_ARGS_FILE"] = str(docker_args)

        result = subprocess.run(
            ["bash", str(RENDER_SCRIPT), algorithm, str(output_cfg)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, output_cfg, docker_args

    def test_renderer_substitutes_algorithm_and_runs_config_check(self) -> None:
        for algorithm, haproxy_algorithm in ALGORITHMS.items():
            with self.subTest(algorithm=algorithm), tempfile.TemporaryDirectory() as tmp_dir:
                result, output_cfg, docker_args = self.render(algorithm, Path(tmp_dir))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"balance {haproxy_algorithm}",
                              output_cfg.read_text(encoding="utf-8"))

                args = docker_args.read_text(encoding="utf-8").splitlines()
                self.assertIn("haproxy:2.8", args)
                self.assertEqual(args[-4:], [
                    "haproxy",
                    "-c",
                    "-f",
                    "/usr/local/etc/haproxy/haproxy.cfg",
                ])


if __name__ == "__main__":
    unittest.main()
