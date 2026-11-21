# Live UI Relay

The dashboard is a no-build HTML, CSS, and JavaScript presentation at `ui/static/index.html`.

## Start

In WSL/Linux, from the repository root:

```bash
python3 ui/relay.py
```

Open `http://127.0.0.1:8765/` in the Windows or WSL browser.

The relay binds only to `127.0.0.1`. It exposes fixed `/api/*` read and control operations and does not accept browser-supplied command arguments.

## Scope

- U1-U10: live topology and project metrics.
- U11: live result files; checkpoint-2 files are explicitly labeled dry-run and not report data.
- U12: flags unavailable or partial environment information.
- U13: fixed controls for load, b3, and approved RTT values.
- U14: panel timestamps and source names.

The relay reads the existing `/stats` endpoint, Docker topology, logs, routing, netem state, HAProxy statistics, and result files. It does not modify load-balancing code or create backend analytics.
