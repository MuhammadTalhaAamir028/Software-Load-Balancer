# Group guide: run the whole project

For Talha, Sufyan and Abdur. This is the short version. The full commands are in [README.md](README.md) and [START_PROJECT.md](START_PROJECT.md).

## 1. Start up

1. **First time on a PC:** double-click `start.bat` (10 to 25 minutes, may ask for administrator rights and a restart; run it again after the restart).
   **Every other time:** double-click `quick_start.bat` (about 1 minute). Keep its window open; closing it stops the UI.
2. Open <http://127.0.0.1:8765/>. The top right must say "Relay online".
3. Check the lab in WSL (Ubuntu 24.04), from `~/slb`: `bash topo/verify.sh`.
   The traceroute must show `10.0.1.1`, `10.0.2.2`, `10.0.3.11`. If it shows `*` after the first hop, rebuild with `bash topo/build.sh`.

## 2. Precautions (read before you run anything)

- **Clock:** the Windows clock must be in sync (Settings, Time and language, Sync now). A time step of a few seconds in the middle of a run makes the kill-time check fail. Check: `w32tm /stripchart /computer:time.windows.com /samples:2 /dataonly` should show an offset under 0.5 s.
- **Experiments run alone:** close the UI and every heavy program, and run **one system at a time**. "Both (demo)" in the UI is only for watching, never for numbers.
- **Keep WSL alive:** WSL shuts down when nothing runs in it and the lab loses its links. Keep the `quick_start.bat` window or a WSL terminal open.
- **One distro runs Docker:** use `Ubuntu-24.04`; stop Docker in any other distro first. Work in `~/slb`, never in `/mnt/...`.
- **Raw logs live only in the lab VM** (`~/slb/results/exp1`, `exp2`, about 2 GB). Copy them somewhere safe before you reinstall or unregister WSL. Never commit them (`hey_c*.csv`, `lb.jsonl`, `haproxy.log`, `.pcap`).
- **Line endings:** shell and config files must be LF (the repo enforces it). `$'\r': command not found` means a CRLF file.
- **No invented numbers:** every number in the report comes from a scripted run with logs and captures.
- **Git:** pull before you start, make small commits under your own name, test before you push (`python -m unittest discover -s tests -t .` should say OK, 222 tests). Do not edit `lb/interfaces.py`.
- **AI use** is described only in `docs/AI_USE_DISCLOSURE.md`, which the group writes. No AI tool names in commits, code, docs or the report.
- **Clean up when finished:** `bash topo/teardown.sh`, `wsl --shutdown`, and remove any temporary sudo rule you added.

## 3. Run the project in its entirety

1. **Start up** (section 1).
2. **Unit tests** (Windows or Linux): `python -m unittest discover -s tests -t .`
3. **Proof of concept** in the UI, panel U13: Start load, wait 20 s, Kill b3 (DOWN after about 3 s), Restore b3. Try the three algorithms and the delay buttons (20, 100, 200 ms).
4. **Packet captures** (only if you want to redo them): commands and filters are in `captures/README.md`.
5. **72 experiments** (about 2 to 2.5 hours). Close the UI first, then in WSL from `~/slb`:

   ```bash
   ulimit -n 65535
   python3 bench/make_configs.py --out-dir results/configs
   cp results/configs/config.round_robin.json results/lb_config.json
   LB_CONFIG=$PWD/results/lb_config.json bash topo/build.sh
   bash haproxy/render.sh round_robin && docker restart -t 1 clab-lb-haproxy
   SEED=1 bash bench/run_all.sh        # safe to stop and run again; it skips valid runs
   ```

6. **Tables and plots:** `sudo apt install -y python3-matplotlib` once, then `python3 bench/analyze.py`.
7. **Copy the small results into the repo** (summaries, CPU samples, tables, plots, not the raw logs):

   ```bash
   cd ~/slb/results
   rsync -a --prune-empty-dirs --exclude 'hey_c*.csv' --exclude 'hey_c*.err' --exclude 'lb.jsonl' \
     --exclude 'haproxy.log' --exclude '*.log' --exclude 'checkpoint2/' --exclude 'ui/' \
     --exclude 'configs/' --exclude 'lb_config.json' \
     exp1 exp2 tables plots seed.txt /mnt/a/path/to/the/repo/results/
   ```

8. **Report:** put the numbers from `results/tables/*.csv` into `docs/report/Project5_Technical_Report.docx`, check it is 6 to 10 pages, export the PDF.
9. **Commit and push.**

## 4. If something breaks

| What you see | What to do |
|---|---|
| Traceroute shows `*` after hop 1 | `bash topo/build.sh`, then `bash topo/verify.sh` |
| A run says `INVALID: kill at t0+3x s` | The clock stepped. Sync the Windows clock, `wsl --shutdown`, rebuild the lab, run again |
| HAProxy does not answer | `bash haproxy/render.sh round_robin && docker restart -t 1 clab-lb-haproxy` |
| The UI page does not open | Run `quick_start.bat` again |
| Anything else | The troubleshooting table at the end of `START_PROJECT.md` |
