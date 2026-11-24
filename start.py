#!/usr/bin/env python3
"""start.py - set up and run Project 5 on a Windows machine with one command.

WHAT IT DOES (every step checks first, so running it again is safe and fast)
  1. Installs or updates WSL 2 (needs administrator rights and maybe a reboot the first time).
  2. Installs an Ubuntu distro if there is none, and creates a normal Linux user for the lab.
  3. Inside Ubuntu: installs Docker Engine, containerlab, tshark and the small tools the lab needs.
  4. Copies this project into the Linux file system (the lab must not run from /mnt/c).
  5. Builds the client and backend images and pulls the router, HAProxy and Python images.
  6. Deploys the routed topology (R1, R2, own load balancer, HAProxy, 4 clients, 4 backends).
  7. Starts the live UI server in the BACKGROUND and opens http://127.0.0.1:8765/ in your browser.

HOW TO RUN (colleague with this repo pulled)
  Double-click start.bat            (installs Python with winget if it is missing)
  or, in a terminal in this folder:  python start.py

OTHER COMMANDS
  python start.py --status         is it running?
  python start.py --logs           last lines of the server log
  python start.py --stop           stop the UI server and tear the lab down
  python start.py --stop --keep-lab   stop only the UI server
  python start.py --check          report what is installed, change nothing
  python start.py --print-commands print the exact shell commands this script runs inside Ubuntu

OPTIONS
  --distro NAME        use this WSL distro (default: Ubuntu-24.04, the tested version; else any Ubuntu; else installs Ubuntu-24.04)
  --linux-dir PATH     where the project is copied inside Linux (default ~/slb)
  --no-update          skip "wsl --update"
  --no-browser         do not open the browser
  --rebuild            rebuild the client and backend images
  --latest-containerlab   install the newest containerlab (default: 0.79.0, the version this project was tested with)

NOTES
  * First run takes 10 to 25 minutes (downloads and image builds). Later runs take under a minute.
  * The UI server keeps running in the background. Keep your Windows session logged in. Stop it with --stop.
  * Only the Python standard library is used on Windows.
"""

from __future__ import annotations

import argparse
import codecs
import ctypes
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parent
STATE_FILE = ROOT / ".start_state.json"
URL = "http://127.0.0.1:8765/"
DEFAULT_DISTRO = "Ubuntu-24.04"
LAB_USER = "lbuser"
CLAB_TESTED = "0.79.0"
LOG_NAME = "slb-ui.log"            # inside the Linux home of the lab user

# --------------------------------------------------------------------------- scripts that run inside Ubuntu
# Values reach the scripts as exported variables (see wsl_script), never as command-line arguments,
# because wsl.exe re-parses arguments through a shell and would break paths with spaces or backslashes.

# Run as root. Needs: LAB_USER, SET_DEFAULT (yes|no)
SCRIPT_USER = r"""
set -e
if ! id "$LAB_USER" >/dev/null 2>&1; then
  echo "creating Linux user $LAB_USER"
  useradd -m -s /bin/bash "$LAB_USER"
  passwd -l "$LAB_USER" >/dev/null
fi
cfg=/etc/wsl.conf; changed=0; touch "$cfg"
if ! grep -qE '^systemd *= *true' "$cfg"; then
  if grep -q '^\[boot\]' "$cfg"; then sed -i '/^\[boot\]/a systemd=true' "$cfg"; else printf '\n[boot]\nsystemd=true\n' >> "$cfg"; fi
  changed=1
fi
if [ "$SET_DEFAULT" = "yes" ] && ! grep -qE "^default *= *$LAB_USER" "$cfg"; then
  if grep -q '^\[user\]' "$cfg"; then sed -i "/^\[user\]/a default=$LAB_USER" "$cfg"; else printf '\n[user]\ndefault=%s\n' "$LAB_USER" >> "$cfg"; fi
  changed=1
fi
echo "RESULT_CHANGED=$changed"
"""

# Run as root. Needs: LAB_USER, CLAB_VERSION (a version number or "latest")
SCRIPT_TOOLS = r"""
set -eu
export DEBIAN_FRONTEND=noninteractive
restart=0

echo "== apt packages"
apt-get update -qq
echo 'wireshark-common wireshark-common/install-setuid boolean false' | debconf-set-selections
apt-get install -y -qq --no-install-recommends ca-certificates curl git rsync python3 tshark tcpdump \
  iproute2 iputils-ping traceroute unzip gnupg >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  echo "== installing Docker Engine"
  curl -fsSL https://get.docker.com | sh || apt-get install -y -qq docker.io
  restart=1
fi
if ! docker info >/dev/null 2>&1; then
  echo "== starting the Docker service"
  (systemctl enable --now docker 2>/dev/null) || service docker start || true
  for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
docker info >/dev/null 2>&1 || { echo "ERROR: Docker did not start"; exit 1; }

if ! command -v containerlab >/dev/null 2>&1; then
  echo "== installing containerlab ($CLAB_VERSION)"
  if [ "$CLAB_VERSION" = "latest" ]; then curl -sL https://get.containerlab.dev | bash
  else curl -sL https://get.containerlab.dev | bash -s -- -v "$CLAB_VERSION"; fi
  restart=1
fi
command -v containerlab >/dev/null 2>&1 || { echo "ERROR: containerlab was not installed"; exit 1; }

getent group docker >/dev/null || groupadd docker
getent group clab_admins >/dev/null || groupadd clab_admins
for g in docker clab_admins; do
  id -nG "$LAB_USER" | tr ' ' '\n' | grep -qx "$g" || { usermod -aG "$g" "$LAB_USER"; restart=1; }
done

echo "== versions"
docker --version; containerlab version 2>/dev/null | grep -i 'version' | head -1 || true
tshark --version 2>/dev/null | head -1
echo "RESULT_RESTART=$restart"
"""

# Run as root after a restart of the distro. Makes sure the Docker daemon is up.
SCRIPT_DOCKER_UP = r"""
docker info >/dev/null 2>&1 || { systemctl start docker 2>/dev/null || service docker start; }
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 && echo "RESULT_DOCKER=ok"
"""

# Run as the lab user. Needs: SRC (project path as seen from Linux), DEST, REBUILD (yes|no)
SCRIPT_PROJECT = r"""
set -eu
case "$DEST" in "~"*) DEST="$HOME${DEST#\~}";; esac
mkdir -p "$DEST"

echo "== copying the project to $DEST"
rsync -a --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '.start_state.json' \
  --exclude 'logs/' --exclude 'haproxy/haproxy.cfg' --exclude 'topo/.netem_state' \
  --exclude 'captures/*.pcap' --exclude 'results/ui/' --exclude 'results/lb_config.json' \
  --exclude 'results/configs/' --exclude 'results/.lock' \
  --exclude 'results/exp1/' --exclude 'results/exp2/' --exclude 'results/tables/' --exclude 'results/plots/' \
  --exclude 'results/seed.txt' --exclude 'results/checkpoint2/' \
  "$SRC"/ "$DEST"/
cd "$DEST"
# Windows checkouts can add CRLF line endings, which break shell scripts and the .env file.
find . -type f \( -name '*.sh' -o -name '*.py' -o -name '*.json' -o -name '*.yml' -o -name '*.yaml' \
  -o -name '*.tmpl' -o -name '.env' -o -name '*.html' -o -name '*.js' -o -name '*.css' -o -name '*.cfg' -o -name '*.conf' \
  -o -path './topo/frr/*' \) -not -path './.git/*' \
  -exec sed -i 's/\r$//' {} +
chmod +x ui/start.sh topo/*.sh backends/*.sh bench/*.sh haproxy/*.sh 2>/dev/null || true

echo "== container images"
for img in quay.io/frrouting/frr:10.2.1 python:3.11-slim haproxy:2.8; do
  docker image inspect "$img" >/dev/null 2>&1 || { echo "pulling $img"; docker pull -q "$img" >/dev/null; }
done
if [ "$REBUILD" = "yes" ] || ! docker image inspect cn-lb-client:latest >/dev/null 2>&1; then
  echo "building cn-lb-client (hey is built from source, this takes a few minutes)"
  docker build -q -t cn-lb-client:latest bench/client >/dev/null
fi
if [ "$REBUILD" = "yes" ] || ! docker image inspect cn-lb-backend:latest >/dev/null 2>&1; then
  echo "building cn-lb-backend"
  docker build -q -t cn-lb-backend:latest backends >/dev/null
fi
echo "RESULT_DEST=$DEST"
"""

# Run in the background as the lab user. Needs: DEST
SCRIPT_RUN_UI = r"""
cd "$DEST" && (ulimit -n 65535 2>/dev/null || true)
exec bash ui/start.sh >> "$HOME/slb-ui.log" 2>&1
"""


# --------------------------------------------------------------------------- helpers

def say(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def fail(message: str, code: int = 1) -> None:
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(code)


def run(args: list, input_text: str | None = None, stream: bool = False, timeout: float | None = None) -> tuple:
    """Run a command. Returns (return code, output text). With stream=True the output is printed as it arrives.

    wsl.exe itself prints UTF-16, programs inside Linux print UTF-8, so the encoding is detected from the
    first chunk and decoded incrementally (splitting UTF-16 on bytes would cut characters in half).
    """
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    if input_text is not None:
        proc.stdin.write(input_text.replace("\r\n", "\n").encode("utf-8"))
        proc.stdin.close()
    lines: list = []
    decoder = None
    pending = ""
    deadline = time.time() + timeout if timeout else None

    def emit(text: str) -> None:
        text = text.replace("﻿", "").rstrip()
        lines.append(text)
        if stream and text:
            print("    " + text, flush=True)

    while True:
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        if decoder is None:
            decoder = codecs.getincrementaldecoder("utf-16-le" if b"\x00" in chunk else "utf-8")(errors="replace")
        pending += decoder.decode(chunk).replace("\r\n", "\n").replace("\r", "\n")
        *done, pending = pending.split("\n")
        for text in done:
            emit(text)
        if deadline and time.time() > deadline:
            proc.kill()
            break
    if pending:
        emit(pending)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def result(output: str, key: str) -> str:
    for line in output.splitlines():
        if line.startswith(f"RESULT_{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_elevated() -> None:
    params = " ".join(f'"{arg}"' for arg in sys.argv[1:] if arg != "--elevated")
    code = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{pathlib.Path(__file__).resolve()}" {params} --elevated',
                                               str(ROOT), 1)
    if code <= 32:
        fail("Administrator rights are needed to install WSL, and the request was refused.")
    say("A new window with administrator rights was opened. Continue there.")
    sys.exit(0)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(**values) -> None:
    state = load_state()
    state.update(values)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def relay_up() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/status", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# --------------------------------------------------------------------------- WSL layer

def wsl_modern() -> bool:
    if not shutil.which("wsl.exe"):
        return False
    code, out = run(["wsl.exe", "--version"], timeout=30)
    return code == 0 and "WSL" in out


def list_distros() -> list:
    """[(name, wsl_version)] for installed distros, default first, ignoring Docker Desktop's helper distros."""
    code, out = run(["wsl.exe", "-l", "-v"], timeout=30)
    found = []
    for line in out.splitlines()[1:]:
        parts = line.replace("*", " ").split()
        if len(parts) >= 3 and not parts[0].lower().startswith("docker-desktop"):
            entry = (parts[0], parts[-1])
            found.insert(0, entry) if "*" in line else found.append(entry)
    return found


def wsl_script(distro: str, user: str, script: str, env: dict | None = None, stream: bool = True,
               timeout: float | None = None) -> tuple:
    """Run a bash script inside the distro. The script and its variables travel through stdin."""
    header = "".join(f"export {key}={shlex.quote(str(value))}\n" for key, value in (env or {}).items())
    return run(["wsl.exe", "-d", distro, "-u", user, "--", "bash", "-s"], input_text=header + script,
               stream=stream, timeout=timeout)


def ensure_wsl(args) -> None:
    say("Step 1/7: WSL")
    if not wsl_modern():
        if not is_admin():
            say("WSL is not installed. Installing it needs administrator rights...")
            relaunch_elevated()
        say("Installing WSL (this can take a few minutes)...")
        run(["wsl.exe", "--install", "--no-distribution"], stream=True)
        if not wsl_modern():
            fail("WSL was installed but Windows has to restart first.\nRestart the computer, then run start.bat (or python start.py) again.", 3)
    elif not args.no_update:
        say("Updating WSL to the latest version (wsl --update)...")
        run(["wsl.exe", "--update"], stream=True, timeout=600)
    run(["wsl.exe", "--set-default-version", "2"], timeout=60)
    code, out = run(["wsl.exe", "--version"], timeout=30)
    say("WSL ready: " + (out.splitlines()[0].strip() if out else "unknown version"))


def ensure_distro(args) -> str:
    say("Step 2/7: Linux distribution")
    distros = list_distros()
    names = [name for name, _ in distros]
    chosen = None
    if args.distro and args.distro in names:
        chosen = args.distro
    elif not args.distro:
        # Ubuntu 24.04 is the tested version: use it if it is installed, else any Ubuntu, else install 24.04.
        chosen = DEFAULT_DISTRO if DEFAULT_DISTRO in names else next(
            (name for name in names if name.lower().startswith("ubuntu")), None)
    if chosen is None:
        wanted = args.distro or DEFAULT_DISTRO
        say(f"Installing {wanted} (a few minutes)...")
        run(["wsl.exe", "--install", "-d", wanted, "--no-launch"], stream=True, timeout=1800)
        names = [name for name, _ in list_distros()]
        if wanted not in names:
            fail(f"{wanted} did not register. Open '{wanted}' once from the Start menu, finish its user setup, then run this script again.")
        chosen = wanted
    if dict(list_distros()).get(chosen) == "1":
        say(f"Converting {chosen} to WSL 2...")
        run(["wsl.exe", "--set-version", chosen, "2"], stream=True, timeout=1800)
    say(f"Using distro: {chosen}")
    return chosen


def ensure_user(distro: str) -> str:
    """Pick the Linux user for the lab: the distro's normal user, or a new one when the default is root."""
    say("Step 3/7: Linux user and systemd")
    code, out = run(["wsl.exe", "-d", distro, "--", "id", "-un"], timeout=120)
    current = out.strip().splitlines()[-1] if out.strip() else "root"
    user, set_default = (current, "no") if current != "root" else (LAB_USER, "yes")
    code, out = wsl_script(distro, "root", SCRIPT_USER, {"LAB_USER": user, "SET_DEFAULT": set_default}, stream=False)
    if code != 0:
        fail("Could not prepare the Linux user:\n" + out)
    if result(out, "CHANGED") == "1":
        say("WSL settings changed, restarting the distro...")
        run(["wsl.exe", "--terminate", distro], timeout=60)
        time.sleep(3)
    say(f"Lab user: {user}")
    return user


def ensure_tools(distro: str, user: str, clab_version: str) -> None:
    say("Step 4/7: Docker, containerlab, tshark (first run downloads about 500 MB)")
    code, out = wsl_script(distro, "root", SCRIPT_TOOLS, {"LAB_USER": user, "CLAB_VERSION": clab_version})
    if code != 0:
        fail("Installing the tools failed. Read the lines above, fix the problem and run start.py again.")
    if result(out, "RESTART") == "1":
        say("Restarting the distro so the new groups apply...")
        run(["wsl.exe", "--terminate", distro], timeout=60)
        time.sleep(4)
        code, out = wsl_script(distro, "root", SCRIPT_DOCKER_UP, stream=False)
        if result(out, "DOCKER") != "ok":
            fail("Docker did not start after the restart.")


def ensure_project(distro: str, user: str, linux_dir: str, rebuild: bool) -> str:
    say("Step 5/7: copying the project into Linux and building images")
    source = str(ROOT).replace("\\", "/")                      # wslpath accepts forward slashes
    code, out = wsl_script(distro, user, 'wslpath -a "$WINPATH"', {"WINPATH": source}, stream=False)
    wsl_path = out.strip().splitlines()[-1] if out.strip() else ""
    if code != 0 or not wsl_path.startswith("/"):
        fail("Could not translate the project path for WSL: " + out)
    code, out = wsl_script(distro, user, SCRIPT_PROJECT,
                           {"SRC": wsl_path, "DEST": linux_dir, "REBUILD": "yes" if rebuild else "no"})
    dest = result(out, "DEST")
    if code != 0 or not dest:
        fail("Copying the project or building the images failed. Read the lines above.")
    return dest


def start_ui(distro: str, user: str, dest: str, wait_seconds: int) -> None:
    say("Step 6/7: deploying the lab and starting the UI server in the background")
    if relay_up():
        say("The UI server is already running.")
        return
    script = f"export DEST={shlex.quote(dest)}\n" + SCRIPT_RUN_UI
    # No DETACHED_PROCESS: with it wsl.exe never receives the script on stdin (tested). A hidden console
    # plus its own process group keeps the server alive after this window is closed or Ctrl+C is pressed.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(["wsl.exe", "-d", distro, "-u", user, "--", "bash", "-s"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    proc.stdin.write(script.encode("utf-8"))
    proc.stdin.close()
    say(f"Waiting for the server (the first deploy takes a few minutes; log: ~/{LOG_NAME} in Linux)...")
    deadline, last = time.time() + wait_seconds, ""
    while time.time() < deadline:
        if relay_up():
            return
        code, out = wsl_script(distro, user, f'tail -n 2 "$HOME/{LOG_NAME}" 2>/dev/null', stream=False, timeout=30)
        line = out.strip().splitlines()[-1] if out.strip() else ""
        if line and line != last:
            print("    " + line[:150], flush=True)
            last = line
        time.sleep(5)
    code, inside = wsl_script(distro, user, "curl -s -m 3 http://127.0.0.1:8765/api/status", stream=False, timeout=30)
    if '"ok": true' in inside:
        fail("The server runs inside WSL but Windows cannot reach it.\n"
             "Check that %UserProfile%\\.wslconfig does not set localhostForwarding=false, then run: wsl --shutdown")
    fail("The server did not start in time. Run: python start.py --logs")


# --------------------------------------------------------------------------- other commands

def cmd_status() -> None:
    state = load_state()
    print("UI server :", "RUNNING at " + URL if relay_up() else "not running")
    print("WSL       :", "installed" if wsl_modern() else "not installed")
    print("Distros   :", ", ".join(f"{n} (WSL {v})" for n, v in list_distros()) or "none")
    if state.get("distro"):
        code, out = wsl_script(state["distro"], state["user"],
                               "docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^clab-lb-' || true", stream=False, timeout=60)
        print("Lab nodes :", out.strip().splitlines()[-1] if out.strip() else "?", "containers running")


def cmd_logs() -> None:
    state = load_state()
    if not state.get("distro"):
        fail("Nothing has been started yet.")
    code, out = wsl_script(state["distro"], state["user"], f'tail -n 80 "$HOME/{LOG_NAME}"', stream=False, timeout=60)
    print(out)


def cmd_stop(keep_lab: bool) -> None:
    state = load_state()
    if not state.get("distro"):
        fail("Nothing has been started yet.")
    distro, user, dest = state["distro"], state["user"], state.get("dest", "~/slb")
    say("Stopping the UI server...")
    wsl_script(distro, user, "pkill -f 'ui/relay.py' || true", stream=False, timeout=60)
    if not keep_lab:
        say("Tearing the lab down (containerlab destroy)...")
        wsl_script(distro, user, 'cd "$DEST" && bash topo/teardown.sh', {"DEST": dest}, timeout=300)
    say("Stopped.")


def cmd_check() -> None:
    print("Windows build :", sys.getwindowsversion().build)
    print("Administrator :", is_admin())
    print("WSL           :", "installed" if wsl_modern() else "NOT installed")
    distros = list_distros()
    print("Distros       :", ", ".join(f"{n} (WSL {v})" for n, v in distros) or "none")
    name = next((n for n, _ in distros if n.lower().startswith("ubuntu")), None)
    if name:
        code, out = wsl_script(name, "root", 'for t in docker containerlab tshark rsync; do command -v $t >/dev/null '
                               '&& echo "$t: yes" || echo "$t: NO"; done', stream=False, timeout=120)
        print(out)
    print("UI server     :", "running" if relay_up() else "not running")


def print_commands() -> None:
    for title, script in (("as root: user and WSL settings (LAB_USER, SET_DEFAULT)", SCRIPT_USER),
                          ("as root: tools (LAB_USER, CLAB_VERSION)", SCRIPT_TOOLS),
                          ("as the lab user: project and images (SRC, DEST, REBUILD)", SCRIPT_PROJECT),
                          ("as the lab user, in the background: deploy the lab and start the UI (DEST)", SCRIPT_RUN_UI)):
        print(f"\n######## {title}\n{script.strip()}")


# --------------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description="Set up and run Project 5 on Windows.")
    parser.add_argument("--distro")
    parser.add_argument("--linux-dir", default="~/slb")
    parser.add_argument("--no-update", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--latest-containerlab", action="store_true")
    parser.add_argument("--wait", type=int, default=900, help="seconds to wait for the UI server (default 900)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--logs", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--keep-lab", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--print-commands", action="store_true")
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.print_commands:
        return print_commands()
    if sys.platform != "win32":
        fail("start.py is for Windows. On Linux run: bash ui/start.sh")
    if sys.getwindowsversion().build < 19041:
        fail("Windows 10 version 2004 (build 19041) or newer is needed for WSL 2. Update Windows first.")
    if args.status:
        return cmd_status()
    if args.logs:
        return cmd_logs()
    if args.stop:
        return cmd_stop(args.keep_lab)
    if args.check:
        return cmd_check()
    if not (ROOT / "ui" / "start.sh").exists() or not (ROOT / "topo" / "build.sh").exists():
        fail("This folder does not contain the UI (ui/start.sh). Pull the branch that has the UI (feat/ui or main).")

    try:
        ensure_wsl(args)
        distro = ensure_distro(args)
        user = ensure_user(distro)
        ensure_tools(distro, user, "latest" if args.latest_containerlab else CLAB_TESTED)
        dest = ensure_project(distro, user, args.linux_dir, args.rebuild)
        save_state(distro=distro, user=user, dest=dest)
        start_ui(distro, user, dest, args.wait)
        say("Step 7/7: ready")
        print(f"\n  UI: {URL}\n  Stop: python start.py --stop     Logs: python start.py --logs\n")
        if not args.no_browser:
            webbrowser.open(URL)
    finally:
        if args.elevated:
            input("\nPress Enter to close this window...")


if __name__ == "__main__":
    main()
