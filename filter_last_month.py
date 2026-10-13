# -*- coding: utf-8 -*-
"""按发布时间过滤小红书搜索结果，保留近一个月的笔记。

用法: python filter_last_month.py [天数，默认30] [输入jsonl路径，默认data/xhs/jsonl下最新的search_contents_*.jsonl]
输出: 同目录 search_contents_<日期>_last_<N>days.jsonl
"""
import glob
import re
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

if len(sys.argv) > 2:
    src = Path(sys.argv[2])
else:
    candidates = sorted(glob.glob("data/xhs/jsonl/search_contents_*.jsonl"))
    candidates = [c for c in candidates if not re.search(r"_last\d+d\.jsonl$", c)]
    if not candidates:
        sys.exit("no input jsonl found")
    src = Path(candidates[-1])

cutoff = datetime.now() - timedelta(days=DAYS)
cutoff_ms = int(cutoff.timestamp() * 1000)

latest: dict[str, dict] = {}
order: list[str] = []
dropped = 0
with open(src, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        nid = rec["note_id"]
        if nid not in latest:  # jsonl store 是追加式存储，同一笔记保留最新一条
            order.append(nid)
        latest[nid] = rec

kept = []
for nid in order:
    rec = latest[nid]
    ts = rec.get("time") or rec.get("last_update_time") or 0
    if ts >= cutoff_ms:
        kept.append(rec)
    else:
        dropped += 1

    # 输出按 日期_时分 命名（时间插在 lastNd 后缀前，保持 *_lastNd.jsonl 约定），避免同日覆盖
    hhmm = datetime.now().strftime("%H%M")
    dst = src.with_name(f"{src.stem}_{hhmm}_last{DAYS}d.jsonl")
with open(dst, "w", encoding="utf-8") as f:
    for rec in kept:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

print(f"input        : {src}")
print(f"cutoff       : >= {cutoff:%Y-%m-%d %H:%M:%S} ({cutoff_ms})")
print(f"total unique : {len(latest)}")
print(f"kept ({DAYS}d) : {len(kept)}")
print(f"dropped      : {dropped}")
print(f"output       : {dst}")
if kept:
    kept_sorted = sorted(kept, key=lambda r: r.get("time") or 0, reverse=True)
    times = sorted(datetime.fromtimestamp((r.get('time') or 0) / 1000) for r in kept)
    print(f"kept range   : {times[0]:%Y-%m-%d %H:%M} ~ {times[-1]:%Y-%m-%d %H:%M}")
    for r in kept_sorted[:8]:
        t = datetime.fromtimestamp((r.get("time") or 0) / 1000)
        title = (r.get("title") or r.get("desc") or "")[:40].replace("\n", " ")
        print(f"  [{t:%m-%d %H:%M}] {r['note_id']} {title}")
    if len(kept_sorted) > 8:
        print(f"  ... and {len(kept_sorted) - 8} more (newest first)")
