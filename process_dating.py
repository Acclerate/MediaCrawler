# -*- coding: utf-8 -*-
"""交友类数据后处理：剔除男生发帖 + 提取评论区女生自我介绍（含其主页地址）。

性别判定（权威优先，文本回退）：
  1. 作者主页 userPageData.basicInfo.gender：1=女 2=男 0=未设置（结果缓存 gender_cache.json）
  2. 未设置/获取失败时，按笔记标题+正文文本特征判定（仅强男性特征才剔除，其余保留并标记）

评论区女生自荐判定：
  - 评论内容含女性自述特征（女生/本人女/女+年龄坐标等）
  - 且能通过其主页 gender 或文本确认非男生
  - 记录评论者昵称、主页地址（author_homepage）、评论原文

用法: python process_dating.py [备注]
输入: data/xhs/jsonl/search_contents_<最新日期>.jsonl + search_comments_<同日>.jsonl
输出: data/xhs/dating_处理结果.xlsx（Sheet: 保留帖子 / 女生自荐评论）+ dating_processed_<日期>.jsonl
"""
import asyncio
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright

from media_platform.xhs.client import XiaoHongShuClient
from tools import utils as mc_utils

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
GENDER_CACHE = Path("gender_cache.json")
JSONL_DIR = Path("data/xhs/jsonl")
OUT_DIR = Path("data/xhs")
PROBE_INTERVAL_SEC = 2  # 主页探测间隔，保持礼貌

# 文本特征（仅用于 gender=0 未设置时的回退；MALE_STRONG 命中才剔除）
MALE_STRONG = [r"男找女", r"本人男", r"男生[，, ]", r"\d{2}年男", r"我男[的生]"]
FEMALE_HINT = [r"女找男", r"本人女", r"女生[，, ]", r"\d{2}年女", r"姐妹", r"小姐[姐]"]
# 评论区女生自荐特征：女性标记 + 自我介绍要素
FEMALE_SELFINTRO = [r"女生", r"本人女", r"女[，,、 ]", r"姐妹", r"姐姐"]
SELFINTRO_ELEM = [r"\d{2}年", r"\d{2}岁", r"坐标", r"身高", r"\d{3}cm", r"私[信信联]", r"认识", r"有意", r"联系", r"蹲"]


def load_cache() -> dict:
    return json.loads(GENDER_CACHE.read_text(encoding="utf-8")) if GENDER_CACHE.exists() else {}


def save_cache(cache: dict) -> None:
    GENDER_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def text_gender(title_desc: str) -> str:
    """文本回退判定：male_strong(剔除) / female_hint(保留) / unknown(保留并标记)"""
    if any(re.search(p, title_desc) for p in MALE_STRONG):
        return "male_text"
    if any(re.search(p, title_desc) for p in FEMALE_HINT):
        return "female_text"
    return "unknown"


def is_female_selfintro(content: str) -> bool:
    if not content:
        return False
    if any(re.search(p, content) for p in MALE_STRONG):
        return False
    has_female = any(re.search(p, content) for p in FEMALE_SELFINTRO)
    has_intro = any(re.search(p, content) for p in SELFINTRO_ELEM)
    return has_female and has_intro


def load_notes() -> tuple[list[dict], Path]:
    files = [f for f in glob.glob(str(JSONL_DIR / "search_contents_*.jsonl")) if not re.search(r"_last\d+d", f)]
    dated = [f for f in files if re.search(r"search_contents_\d{4}-\d{2}-\d{2}\.jsonl$", f)]
    src = Path(max(dated)) if dated else Path(max(files))  # 按日期取最新，避免旧导出文件干扰
    latest: dict[str, dict] = {}
    for line in src.open(encoding="utf-8"):
        r = json.loads(line)
        latest[r["note_id"]] = r  # 同笔记保留最新
    return list(latest.values()), src


def load_comments() -> dict[str, list[dict]]:
    """返回 note_id -> [comment, ...]"""
    files = sorted(glob.glob(str(JSONL_DIR / "search_comments_*.jsonl")))
    if not files:
        return {}
    comments: dict[str, dict] = {}
    for fp in files:  # 跨天合并，同评论保留最新
        for line in open(fp, encoding="utf-8"):
            c = json.loads(line)
            comments[c["comment_id"]] = c
    by_note: dict[str, list[dict]] = {}
    for c in comments.values():
        by_note.setdefault(c["note_id"], []).append(c)
    return by_note


async def probe_genders(user_ids: list[str]) -> dict:
    """批量探测用户主页 gender（带缓存）。返回 {user_id: {"gender": 0/1/2, "ip": str}}"""
    cache = load_cache()
    todo = [u for u in user_ids if u and u not in cache]
    print(f"性别缓存: {len(cache)} 已有, {len(todo)} 需探测")
    if not todo:
        return cache

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=os.path.join(os.getcwd(), "browser_data", "xhs_user_data_dir"),
            headless=False, viewport={"width": 1920, "height": 1080}, user_agent=UA)
        page = await ctx.new_page()
        await page.goto("https://www.xiaohongshu.com")
        cookie_str, cookie_dict = await mc_utils.convert_browser_context_cookies(
            ctx, urls=["https://www.xiaohongshu.com"])
        client = XiaoHongShuClient(
            headers={"accept": "application/json, text/plain, */*", "user-agent": UA, "Cookie": cookie_str},
            playwright_page=page, cookie_dict=cookie_dict)
        try:
            for i, uid in enumerate(todo, 1):
                try:
                    info = await client.get_creator_info(uid)
                    if info and info.get("result", {}).get("success"):
                        b = info.get("basicInfo", {})
                        cache[uid] = {"gender": int(b.get("gender") or 0), "ip": b.get("ipLocation", ""), "nickname": b.get("nickname", "")}
                    else:
                        cache[uid] = {"gender": -1, "ip": "", "nickname": ""}  # -1=获取失败
                except Exception as e:
                    cache[uid] = {"gender": -1, "ip": "", "nickname": ""}
                    print(f"  探测失败 {uid}: {type(e).__name__}")
                print(f"  [{i}/{len(todo)}] {uid} -> gender={cache[uid]['gender']}")
                if i % 10 == 0:
                    save_cache(cache)  # 中途落盘防丢失
                await asyncio.sleep(PROBE_INTERVAL_SEC)
        finally:
            save_cache(cache)
            await ctx.close()
    return cache


def gender_label(g: int, title_desc: str) -> tuple[str, bool]:
    """返回 (标签, 是否保留)。男(2或强男文本)剔除；女/未设置/失败保留"""
    if g == 2:
        return "男(主页)", False
    if g == 1:
        return "女(主页)", True
    t = text_gender(title_desc)
    if t == "male_text":
        return "男(文本)", False
    if t == "female_text":
        return "女(文本)", True
    return "未知", True


def main() -> None:
    notes, src = load_notes()
    date_str = src.stem.split("_")[-1]
    print(f"笔记来源: {src.name} | 唯一笔记: {len(notes)}")

    # 1) 发帖人性别过滤
    author_ids = list({r["user_id"] for r in notes if r.get("user_id")})
    cache = asyncio.run(probe_genders(author_ids))

    kept, dropped = [], []
    for r in notes:
        g = cache.get(r.get("user_id"), {}).get("gender", -1)
        label, keep = gender_label(g, (r.get("title") or "") + " " + (r.get("desc") or ""))
        r["_author_gender"] = label
        (kept if keep else dropped).append(r)
    print(f"\n发帖人过滤: 保留 {len(kept)} 条 / 剔除男生发帖 {len(dropped)} 条")
    for r in dropped:
        print(f"  [剔] {r['_author_gender']} {(r.get('title') or '')[:30]} | {r.get('nickname')}")

    # 2) 评论区女生自荐
    comments_by_note = load_comments()
    intros = []
    commenter_ids = []
    for r in kept:
        for c in comments_by_note.get(r["note_id"], []):
            if is_female_selfintro(c.get("content") or ""):
                commenter_ids.append(c.get("user_id"))
    commenter_cache = asyncio.run(probe_genders(list(set(commenter_ids)))) if commenter_ids else {}

    for r in kept:
        for c in sorted(comments_by_note.get(r["note_id"], []), key=lambda x: x.get("create_time") or 0, reverse=True):
            if not is_female_selfintro(c.get("content") or ""):
                continue
            cg = commenter_cache.get(c.get("user_id"), {}).get("gender", -1)
            if cg == 2:
                continue  # 评论者是男生，跳过
            label = {"1": "女(主页)", "0": "未设置", "-1": "获取失败"}.get(str(cg), "女(文本)")
            intros.append({
                "所属笔记标题": (r.get("title") or "")[:40],
                "笔记关键词": r.get("source_keyword"),
                "note_id": r["note_id"],
                "评论者昵称": c.get("nickname"),
                "评论者主页": c.get("author_homepage"),
                "性别判定": label,
                "评论内容": (c.get("content") or "")[:120],
                "评论时间": datetime.fromtimestamp((c.get("create_time") or 0) / 1000).strftime("%m-%d %H:%M"),
                "评论点赞": c.get("like_count", 0),
            })

    # 3) 导出
    cutoff = datetime.now() - timedelta(days=30)
    rows = []
    for r in sorted(kept, key=lambda x: x.get("time") or 0, reverse=True):
        t = datetime.fromtimestamp((r.get("time") or 0) / 1000)
        rows.append({
            "关键词": r.get("source_keyword"), "发布时间": t.strftime("%Y-%m-%d %H:%M"),
            "近30天": "是" if t >= cutoff else "否", "标题": (r.get("title") or "")[:50],
            "作者": r.get("nickname"), "作者性别": r["_author_gender"],
            "作者主页": r.get("author_homepage"), "赞": r.get("liked_count") or 0,
            "评论": r.get("comment_count") or 0, "原帖链接": r.get("note_url"),
            "note_id": r.get("note_id"),
        })
    xlsx = OUT_DIR / f"dating_处理结果_{date_str}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        pd.DataFrame(rows).to_excel(w, sheet_name="保留帖子(非男生发帖)", index=False)
        pd.DataFrame(intros).to_excel(w, sheet_name="评论区女生自荐", index=False)
    jsonl_out = JSONL_DIR / f"dating_processed_{date_str}.jsonl"
    with jsonl_out.open("w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n评论区女生自荐: {len(intros)} 条")
    print(f"输出: {xlsx}")
    print(f"输出: {jsonl_out}")
    for x in intros[:8]:
        print(f"  [{x['评论时间']}] {x['评论者昵称']}: {x['评论内容'][:40]}")


if __name__ == "__main__":
    main()
