# -*- coding: utf-8 -*-
"""登录态体检（pong 复测）：带死亡窗口强制校验，结果记录到 crawl_runs.log。

规则：距 crawl_runs.log 中最近一次 CRAWL 的 end 时间必须 >= 死亡窗口（默认 19 分钟），
否则拒绝执行（可用 --force 跳过，但不建议——窗口内复测无法证明会话跨窗口存活）。

用法:
  python diag_cookie.py            # 校验窗口后执行 pong，记录结果
  python diag_cookie.py --force    # 跳过窗口校验（仅调试用）

crawl_runs.log 行格式:
  PONG  time=2026-10-12 23:05:00 since_last_crawl=22m45s(>=19m OK) result=True
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from media_platform.xhs.client import XiaoHongShuClient
from tools import utils as mc_utils

DEATH_WINDOW_MIN = 19  # 观察到的会话死亡窗口上限（分钟），复测必须超过它
USER_DATA_DIR = os.path.join(os.getcwd(), "browser_data", "xhs_user_data_dir")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
RUN_LOG = Path(__file__).parent / "crawl_runs.log"


def last_crawl_end() -> datetime | None:
    """读取 crawl_runs.log 中最近一次 CRAWL 的 end 时间。"""
    if not RUN_LOG.exists():
        return None
    end = None
    for line in RUN_LOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("CRAWL") and "end=" in line:
            try:
                end = datetime.strptime(line.split("end=")[1].split()[0] + " " + line.split("end=")[1].split()[1], "%Y-%m-%d %H:%M:%S")
            except (IndexError, ValueError):
                pass
    return end


def main_sync() -> None:
    force = "--force" in sys.argv
    last_end = last_crawl_end()
    now = datetime.now()
    if last_end is None:
        print("crawl_runs.log 无 CRAWL 记录，无窗口基准，直接执行。")
    else:
        elapsed = (now - last_end).total_seconds() / 60
        if elapsed < DEATH_WINDOW_MIN and not force:
            print(f"拒绝执行：距上次爬取结束仅 {elapsed:.1f} 分钟 < 死亡窗口 {DEATH_WINDOW_MIN} 分钟。")
            print(f"上次爬取结束于 {last_end:%H:%M:%S}，请在 {last_end:%H:%M} + {DEATH_WINDOW_MIN}min 之后再测。")
            sys.exit(2)
        window_note = f"{elapsed:.0f}m({'>=' if elapsed >= DEATH_WINDOW_MIN else '<'}{DEATH_WINDOW_MIN}m {'OK' if elapsed >= DEATH_WINDOW_MIN else 'FORCE'})"
    pong = asyncio.run(run_pong())
    line = f"PONG  time={now:%Y-%m-%d %H:%M:%S} since_last_crawl={window_note if last_end else 'n/a'} result={pong}\n"
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print("\n" + line.strip())
    sys.exit(0 if pong else 1)


async def run_pong() -> bool:
    async with async_playwright() as playwright:
        browser_context = await playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            accept_downloads=True,
            headless=False,
            viewport={"width": 1920, "height": 1080},
            user_agent=UA,
        )
        page = await browser_context.new_page()
        await page.goto("https://www.xiaohongshu.com")

        cookie_str, cookie_dict = await mc_utils.convert_browser_context_cookies(
            browser_context, urls=["https://www.xiaohongshu.com"]
        )
        print("web_session present:", "web_session" in cookie_dict)

        client = XiaoHongShuClient(
            headers={
                "accept": "application/json, text/plain, */*",
                "user-agent": UA,
                "Cookie": cookie_str,
            },
            playwright_page=page,
            cookie_dict=cookie_dict,
        )
        print("query_self response:")
        try:
            print((await client.query_self()).get("msg"))
        except Exception as e:
            print("query_self raised:", type(e).__name__, e)
        pong = await client.pong()
        print("PONG RESULT:", pong)
        await browser_context.close()
        return pong


if __name__ == "__main__":
    main_sync()
