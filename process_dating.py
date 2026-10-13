# -*- coding: utf-8 -*-
"""交友类数据后处理 v2：自称女生豁免 + 历史去重 + IP 限制湖北(武汉)。

规则（依据 2026-10-13 用户反馈）：
  R0 人工复核台账（manual_verdicts.json）优先级最高：判女→保留(人工确认女)、判男→剔除(人工剔除)，
     笔记侧与评论侧均生效；结论在待复核 Sheet 的"复核结论"列填写后由 --collect-verdicts 回收。
  R1 男生主页的帖子：内容"自称女生"（女找男/本人女/XX年女等）→ 保留并标记；否则剔除。
     注意区分"自称女生"与"找女生"（男找女/蹲女生/找个女生）——后者不构成豁免。
  R2 历史去重：已推送过的帖子（note_id）/已报告过的评论者（user_id）不再重复输出。
  R3 IP 限制武汉：作者主页 ipLocation 与笔记自身 ip_location 均需为 湖北/武汉
     （小红书 IP 属地为省级粒度，武汉落地为湖北）；女生自荐评论者主页 IP 同样限湖北。

性别判定数据源：作者主页 userPageData.basicInfo.gender（1=女 2=男 0=未设置，缓存 gender_cache.json）。
规则本体（正则表 + 纯函数 judge_note 等）在 dating_rules.py，本文件只保留流程编排。

用法: python process_dating.py [--seed | --collect-verdicts [xlsx路径]]
       --seed 仅初始化历史种子，不输出
输出: data/xhs/dating_处理结果_<日期_时分>.xlsx
      Sheet1 新增帖子 / Sheet2 女生自荐(新增) / Sheet3 待复核 / Sheet4 剔除明细 / Sheet5 去重与统计
历史: data/xhs/dating_seen_history.json
"""
import asyncio
import glob
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright

from dating_rules import ip_ok, is_female_selfintro, judge_note
from media_platform.xhs.client import XiaoHongShuClient
from tools import utils as mc_utils

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
GENDER_CACHE = Path("gender_cache.json")
HISTORY = Path("data/xhs/dating_seen_history.json")
MV_PATH = Path("data/xhs/manual_verdicts.json")  # 人工复核台账
JSONL_DIR = Path("data/xhs/jsonl")
OUT_DIR = Path("data/xhs")
PROBE_MIN_SEC = 5   # 主页探测间隔下限（随机抖动，去掉等间隔特征）
PROBE_MAX_SEC = 10  # 主页探测间隔上限


def load_cache() -> dict:
    return json.loads(GENDER_CACHE.read_text(encoding="utf-8")) if GENDER_CACHE.exists() else {}


def save_cache(cache: dict) -> None:
    GENDER_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def load_history() -> dict:
    if HISTORY.exists():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return {"note_ids": [], "commenters": {}}


def save_history(h: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


def seed_history() -> dict:
    """用历史输出初始化去重种子：旧组合导出(48) + 10-13处理结果(24) + 已报告评论者(2)。"""
    h = load_history()
    n0, c0 = len(h["note_ids"]), len(h["commenters"])
    for fp in ["data/xhs/jsonl/backup_组合关键词_2026-10-12.jsonl",
               "data/xhs/jsonl/dating_processed_2026-10-13.jsonl"]:
        p = Path(fp)
        if p.exists():
            for line in p.open(encoding="utf-8"):
                r = json.loads(line)
                h["note_ids"].append(r["note_id"])
    for uid, nick in [("5d0651bf000000001602e92f", "有点甜"), ("5eb5988b0000000001006980", "带纸了吗")]:
        h["commenters"][uid] = nick
    h["note_ids"] = sorted(set(h["note_ids"]))
    save_history(h)
    print(f"历史种子: 帖子 {n0}->{len(h['note_ids'])} / 评论者 {c0}->{len(h['commenters'])}")
    return h


def load_verdicts() -> dict[str, dict]:
    """人工复核台账：{uid: {"verdict": "female"|"male", "at": "YYYY-MM-DD"}}。

    兼容旧格式 {uid: "male"}（读取时迁移为 {"verdict": ..., "at": ""}）；
    collect_verdicts 保存时统一落为新格式。
    """
    if not MV_PATH.exists():
        return {}
    raw = json.loads(MV_PATH.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for uid, v in raw.items():
        if isinstance(v, str):  # 旧格式
            out[uid] = {"verdict": v, "at": ""}
        elif isinstance(v, dict) and v.get("verdict") in ("female", "male"):
            out[uid] = {"verdict": v["verdict"], "at": v.get("at", "")}
    return out


def collect_verdicts(xlsx_arg: str | None) -> None:
    """读待复核 Sheet 中人工填写的结论（user_id + 复核结论列），回写 manual_verdicts.json。"""
    if xlsx_arg:
        src = Path(xlsx_arg)
        if not src.exists():
            print(f"文件不存在: {src}")
            return
    else:
        files = sorted(glob.glob(str(OUT_DIR / "dating_处理结果_*.xlsx")))
        if not files:
            print("未找到 dating_处理结果_*.xlsx，请显式指定 xlsx 路径")
            return
        src = Path(files[-1])
    df = pd.read_excel(src, sheet_name="待复核")
    verdicts = load_verdicts()
    n = 0
    for _, row in df.iterrows():
        uid = str(row.get("user_id") or "").strip()
        v = str(row.get("复核结论(填female/male)") or "").strip().lower()
        if uid and v in ("female", "male") and verdicts.get(uid, {}).get("verdict") != v:
            verdicts[uid] = {"verdict": v, "at": datetime.now().strftime("%Y-%m-%d")}
            n += 1
    MV_PATH.parent.mkdir(parents=True, exist_ok=True)
    MV_PATH.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"复核结论收录 {n} 条（来源 {src.name}）→ {MV_PATH}（台账共 {len(verdicts)} 人）")


def load_notes() -> tuple[list[dict], Path]:
    files = [f for f in glob.glob(str(JSONL_DIR / "search_contents_*.jsonl")) if not re.search(r"_last\d+d", f)]
    dated = [f for f in files if re.search(r"search_contents_\d{4}-\d{2}-\d{2}\.jsonl$", f)]
    src = Path(max(dated)) if dated else Path(max(files))
    latest: dict[str, dict] = {}
    for line in src.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("source_keyword") == "武汉":  # 裸城市关键词的笔记不属于交友专题
            continue
        latest[r["note_id"]] = r
    return list(latest.values()), src


def load_comments() -> dict[str, list[dict]]:
    by_note: dict[str, dict] = {}
    for fp in glob.glob(str(JSONL_DIR / "search_comments_*.jsonl")):
        for line in open(fp, encoding="utf-8"):
            c = json.loads(line)
            by_note[c["comment_id"]] = c
    grouped: dict[str, list[dict]] = {}
    for c in by_note.values():
        grouped.setdefault(c["note_id"], []).append(c)
    return grouped


async def probe_genders(user_ids: list[str]) -> dict:
    cache = load_cache()
    todo = [u for u in user_ids if u and u not in cache]
    print(f"性别/IP 缓存: {len(cache)} 已有, {len(todo)} 需探测")
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
                        cache[uid] = {"gender": -1, "ip": "", "nickname": ""}
                except Exception as e:
                    cache[uid] = {"gender": -1, "ip": "", "nickname": ""}
                    print(f"  探测失败 {uid}: {type(e).__name__}")
                print(f"  [{i}/{len(todo)}] {uid} -> gender={cache[uid]['gender']} ip={cache[uid]['ip'] or '?'}")
                if i % 10 == 0:
                    save_cache(cache)
                await asyncio.sleep(random.uniform(PROBE_MIN_SEC, PROBE_MAX_SEC))  # 5~10s 随机抖动，去掉等间隔特征
        finally:
            save_cache(cache)
            await ctx.close()
    return cache


def main() -> None:
    if "--seed" in sys.argv:
        seed_history()
        return
    if "--collect-verdicts" in sys.argv:
        i = sys.argv.index("--collect-verdicts")
        nxt = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
        collect_verdicts(None if nxt is None or nxt.startswith("--") else nxt)
        return
    history = seed_history()
    seen_notes = set(history["note_ids"])

    notes, src = load_notes()
    date_str = src.stem.split("_")[-1]
    print(f"笔记来源: {src.name} | 交友关键词唯一笔记: {len(notes)}")

    author_ids = list({r["user_id"] for r in notes if r.get("user_id")})
    cache = asyncio.run(probe_genders(author_ids))

    kept, reviewed, dropped, deduped = [], [], [], []
    review_seen = 0  # 待复核但已在历史中的帖子（不再重复提示）
    # 人工复核台账优先级最高：判女→保留 / 判男→剔除，优先于一切规则
    verdicts = load_verdicts()
    for r in notes:
        info = cache.get(r.get("user_id"), {})
        v = verdicts.get(r.get("user_id"), {}).get("verdict")
        if v == "female":
            disp, label, basis = "keep", "人工确认女", "人工复核确认女生"
        elif v == "male":
            disp, label, basis = "drop", "人工剔除", "人工复核确认男生"
        else:
            disp, label, basis = judge_note(r, info.get("gender", -1), info.get("ip", ""))
        r["_author_gender"] = label if disp == "keep" else ""
        r["_review_hint"] = f"{label}｜依据:{basis}" if disp == "review" else ""
        r["_drop_reason"] = basis if disp == "drop" else ""
        if disp == "drop":
            dropped.append(r)
        elif disp == "review":
            if r["note_id"] not in seen_notes:
                reviewed.append(r)  # 待复核不进历史，未处理会在下次运行再次出现
            else:
                review_seen += 1
        elif r["note_id"] in seen_notes:
            deduped.append(r)
        else:
            kept.append(r)
    print(f"\n规则过滤: 保留 {len(kept)} 新增 + 待复核 {len(reviewed)} + 历史去重 {len(deduped)} + 剔除 {len(dropped)}")

    # 评论区女生自荐（仅新增帖；评论者去重 + IP 限制）
    comments_by_note = load_comments()
    cand_ids = []
    for r in kept:
        for c in comments_by_note.get(r["note_id"], []):
            if is_female_selfintro(c.get("content") or ""):
                cand_ids.append(c.get("user_id"))
    ccache = asyncio.run(probe_genders(list(set(cand_ids)))) if cand_ids else {}

    intros = []
    for r in kept:
        for c in sorted(comments_by_note.get(r["note_id"], []), key=lambda x: x.get("create_time") or 0, reverse=True):
            if not is_female_selfintro(c.get("content") or ""):
                continue
            uid = c.get("user_id")
            ci = ccache.get(uid, {})
            cg, cip = ci.get("gender", -1), ci.get("ip", "")
            cv = verdicts.get(uid, {}).get("verdict")
            if cv == "male":
                continue  # 人工确认男生：直接跳过
            if cg == 2 and cv != "female":
                continue  # 男生评论者不收录（评论区不适用自称豁免；人工判女除外）
            if cip and not ip_ok(cip):
                continue  # R3: 评论者主页 IP 限制
            if uid in history["commenters"]:
                continue  # R2: 已报告过的评论者
            label = "人工确认女" if cv == "female" else {"1": "女(主页)", "0": "未设置", "-1": "获取失败"}.get(str(cg), "女(文本)")
            intros.append({
                "所属笔记标题": (r.get("title") or "")[:40],
                "笔记关键词": r.get("source_keyword"),
                "note_id": r["note_id"],
                "评论者昵称": c.get("nickname"),
                "评论者主页": c.get("author_homepage"),
                "评论者IP": cip or "?",
                "性别判定": label,
                "评论内容": (c.get("content") or "")[:120],
                "评论时间": datetime.fromtimestamp((c.get("create_time") or 0) / 1000).strftime("%m-%d %H:%M"),
                "评论点赞": c.get("like_count", 0),
            })

    # 导出
    cutoff = datetime.now() - timedelta(days=30)

    def post_row(r: dict, extra: dict | None = None) -> dict:
        t = datetime.fromtimestamp((r.get("time") or 0) / 1000)
        row = {
            "关键词": r.get("source_keyword"), "发布时间": t.strftime("%Y-%m-%d %H:%M"),
            "近30天": "是" if t >= cutoff else "否", "标题": (r.get("title") or "")[:50],
            "作者": r.get("nickname"), "性别判定": r.get("_author_gender") or "",
            "笔记IP": r.get("ip_location") or "(未记录)",
            "作者主页": r.get("author_homepage"), "赞": r.get("liked_count") or 0,
            "评论": r.get("comment_count") or 0, "原帖链接": r.get("note_url"), "note_id": r.get("note_id"),
        }
        if extra:
            row.update(extra)
        return row

    rows = [post_row(r) for r in sorted(kept, key=lambda x: x.get("time") or 0, reverse=True)]
    review_rows = [post_row(r, {"复核提示": r.get("_review_hint", ""),
                                "user_id": r.get("user_id"),
                                "复核结论(填female/male)": ""})  # 人工填写后经 --collect-verdicts 回流
                   for r in sorted(reviewed, key=lambda x: x.get("time") or 0, reverse=True)]
    drop_rows = [{
        "标题": (r.get("title") or "")[:40], "作者": r.get("nickname"),
        "剔除原因": r["_drop_reason"], "原帖链接": r.get("note_url"),
    } for r in dropped]
    stats = pd.DataFrame([
        {"统计项": "输入笔记", "数量": len(notes)},
        {"统计项": "剔除(性别/IP)", "数量": len(dropped)},
        {"统计项": "待复核(男号中性/软信号)", "数量": len(reviewed)},
        {"统计项": "待复核但历史已见(不重复提示)", "数量": review_seen},
        {"统计项": "历史已推送(去重)", "数量": len(deduped)},
        {"统计项": "新增输出", "数量": len(kept)},
        {"统计项": "女生自荐(新增评论者)", "数量": len(intros)},
        {"统计项": "历史累计已推送帖子", "数量": len(seen_notes) + len(kept)},
    ])
    run_stamp = datetime.now().strftime("%Y-%m-%d_%H%M")  # 输出按 日期_时分 命名，避免同日覆盖
    xlsx = OUT_DIR / f"dating_处理结果_{run_stamp}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        pd.DataFrame(rows).to_excel(w, sheet_name="新增帖子", index=False)
        pd.DataFrame(review_rows).to_excel(w, sheet_name="待复核", index=False)
        pd.DataFrame(intros).to_excel(w, sheet_name="女生自荐(新增)", index=False)
        pd.DataFrame(drop_rows).to_excel(w, sheet_name="剔除明细", index=False)
        stats.to_excel(w, sheet_name="去重与统计", index=False)
    jsonl_out = JSONL_DIR / f"dating_processed_{run_stamp}.jsonl"
    with jsonl_out.open("w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # R2: 更新历史（仅确认输出的帖子；待复核不进历史，未处理会在下次运行再次出现）
    history["note_ids"] = sorted(set(history["note_ids"]) | {r["note_id"] for r in kept})
    for x in intros:
        history["commenters"][x["评论者主页"].rstrip("/").split("/")[-1]] = x["评论者昵称"]
    save_history(history)

    print(f"\n新增帖子 {len(kept)} | 待复核 {len(reviewed)} | 女生自荐(新增) {len(intros)} | 剔除 {len(dropped)} | 去重 {len(deduped)}")
    print(f"输出: {xlsx}")
    print(f"输出: {jsonl_out}")
    for r in rows[:6]:
        print(f"  [新增] {r['性别判定']:8s} {r['标题'][:32]} | {r['作者']} | 笔记IP={r['笔记IP']}")
    for r in review_rows[:8]:
        print(f"  [复核] {r['复核提示'][:44]} | {r['标题'][:26]} | {r['作者']}")
    for x in intros[:5]:
        print(f"  [自荐] {x['评论者昵称']} ({x['性别判定']}, IP={x['评论者IP']}): {x['评论内容'][:36]}")


if __name__ == "__main__":
    main()
