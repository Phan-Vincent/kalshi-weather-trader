#!/usr/bin/env python3
"""Concurrency/chaos QA (2026-07-01 specialized QA, Pass B).

Real cross-PROCESS contention on the atomic writers: append_jsonl_atomic (QA-17 flock) and
PaperBook._save (QA-18/19 tmp + os.replace + per-process tmp names). Workers are spawned as
separate OS processes (subprocess), which is what the mutex/atomicity fixes actually defend
against — threads wouldn't exercise the cross-process file race. No network; temp dirs only.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_PATHS = [str(ROOT), str(ROOT.parent), str(ROOT / "bin")]
sys.path[:0] = _PATHS
PY = sys.executable


def _spawn(script, args_list, timeout=120):
    procs = [subprocess.Popen([PY, "-c", script, *a]) for a in args_list]
    rcs = []
    for p in procs:
        try:
            rcs.append(p.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            p.kill()
            rcs.append(-1)
    return rcs


def test_append_jsonl_atomic_no_interleaving_across_processes(tmp_path):
    p = tmp_path / "settlement-log.jsonl"
    W, N = 8, 250
    script = f"""
import sys
sys.path[:0] = {_PATHS!r}
from data.weather_data import append_jsonl_atomic
wid = int(sys.argv[1])
for j in range({N}):
    append_jsonl_atomic({str(p)!r}, {{"w": wid, "j": j}})
"""
    rcs = _spawn(script, [[str(w)] for w in range(W)])
    assert all(rc == 0 for rc in rcs), f"worker return codes {rcs}"
    rows = [json.loads(line) for line in open(p) if line.strip()]   # every line must parse (no tearing)
    assert len(rows) == W * N, f"{len(rows)} rows, expected {W * N} (lost/torn appends)"
    assert {(r["w"], r["j"]) for r in rows} == {(w, j) for w in range(W) for j in range(N)}


def test_concurrent_book_saves_never_torn(tmp_path):
    from trader.paper_book import PaperBook
    p = tmp_path / "paper-book.json"
    PaperBook(state_path=p)._save()   # seed a valid book

    writer = f"""
import sys
sys.path[:0] = {_PATHS!r}
from pathlib import Path
from trader.paper_book import PaperBook
wid = int(sys.argv[1])
b = PaperBook(state_path=Path({str(p)!r}))
for j in range(200):
    b.cash_cents = wid * 100000 + j
    b._save()
"""
    reader = f"""
import json
errs = 0
for _ in range(6000):
    try:
        with open({str(p)!r}) as f:
            txt = f.read()
        if txt.strip():
            json.loads(txt)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        errs += 1
    except Exception:
        pass
print(errs)
"""
    rproc = subprocess.Popen([PY, "-c", reader], stdout=subprocess.PIPE, text=True)
    wrcs = _spawn(writer, [[str(w)] for w in range(4)])
    out, _ = rproc.communicate(timeout=120)
    assert all(rc == 0 for rc in wrcs), f"writer return codes {wrcs}"
    assert rproc.returncode == 0
    torn = int((out or "0").strip() or "0")
    assert torn == 0, f"{torn} torn reads — os.replace atomicity broken under contention"
    json.loads(open(p).read())   # final file is valid JSON (last-writer-wins, not corrupt)
