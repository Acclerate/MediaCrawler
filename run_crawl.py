# -*- coding: utf-8 -*-
"""爬取运行包装器：每次爬取自动记录起止时间戳到 crawl_runs.log。

用法:
  python run_crawl.py              # 等价于 python main.py，并记录时间戳
  python run_crawl.py --lt cookie  # 透传任意 main.py 参数

crawl_runs.log 行格式:
  CRAWL start=2026-10-12 22:40:30 end=2026-10-12 22:42:15 duration=105s exit=0 notes=40
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RUN_LOG = Path(__file__).parent / "crawl_runs.log"
RAW_JSONL_DIR = Path(__file__).parent / "data" / "xhs" / "jsonl"


def count_new_notes(start_ts: float) -> int:
    """统计本次运行期间 jsonl 追加的笔记数（按 mtime 晚于 start 的行数近似）。"""
    total = 0
    for f in RAW_JSONL_DIR.glob("search_contents_*.jsonl"):
        if f.stat().st_mtime < start_ts or "last" in f.stem:
            continue
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if '"last_modify_ts"' in line and '"time"' in line:
                    try:
                        if int(line.split('"last_modify_ts": ')[1].split(",")[0].rstrip("}")) >= start_ts * 1000:
                            total += 1
                    except (IndexError, ValueError):
                        pass
    return total


def main() -> None:
    cmd = [sys.executable, "main.py", *sys.argv[1:]]
    start = time.time()
    print(f"[run_crawl] start {datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S}: {' '.join(cmd)}")
    proc = subprocess.run(cmd)
    end = time.time()
    notes = count_new_notes(start) if proc.returncode == 0 else -1
    line = (
        f"CRAWL start={datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S}"
        f" end={datetime.fromtimestamp(end):%Y-%m-%d %H:%M:%S}"
        f" duration={int(end - start)}s exit={proc.returncode} notes={notes}\n"
    )
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print(f"[run_crawl] {line.strip()}")
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
