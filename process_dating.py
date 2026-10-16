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
  R4 探测后置（§13 风控整改，2026-10-16）：主页探测只花在幸存帖作者身上——
     Stage A 免探测预过滤（台账→历史已见→笔记IP，纯本地数据）→ Stage B 缓存优先探测幸存作者
     → Stage C 规则判决。作者批与评论者批共用一次浏览器会话（ProbeSession）。
     行为变化：已推送帖不再进入剔除/待复核统计（统一计去重）；双 IP 均不合规的
     剔除原因显示"笔记IP"（笔记侧先判）。
  R5 探测可靠性（阶段三，2026-10-16）：探测成功才落缓存（值含 probed_at），失败不写 -1、
     下次运行自动重探；缓存 TTL 30 天（旧 -1 条目视为待重探）；单次运行连续 3 次探测异常
     熔断中止剩余探测；同一作者连续 3 轮失败 → 滞留保护剔除。judge_note：探测失败 →
     待复核(将重试)而非误杀。历史种子仅 --seed 时初始化。
  R6 自荐扫描范围（阶段二，2026-10-16）：女生自荐线索的检测独立于"帖子是否已推送"——
     扫描全部合规帖（新增+已推+待复核）的评论；评论者仍全局只报一次，已报告/人工判男
     的评论者不再探测。

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
import time
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
CACHE_TTL_SEC = 30 * 86400  # 性别/IP 缓存有效期（阶段三：超期重探）
PROBE_BREAKER_N = 3   # 单次运行连续 N 次探测异常 → 熔断中止剩余探测
PROBE_FAIL_DROP_N = 3  # 同一作者连续 N 轮探测失败 → 滞留保护剔除（防止永久滞留待复核）


def load_cache() -> dict:
    if not GENDER_CACHE.exists():
        return {}
    cache = json.loads(GENDER_CACHE.read_text(encoding="utf-8"))
    if any(isinstance(v, dict) and "probed_at" not in v for v in cache.values()):
        # 阶段三缓存结构迁移：旧条目无 probed_at，以缓存文件 mtime 近似最后一次探测时间
        fallback = int(GENDER_CACHE.stat().st_mtime)
        for v in cache.values():
            if isinstance(v, dict) and "probed_at" not in v:
                v["probed_at"] = fallback
    return cache


def save_cache(cache: dict) -> None:
    GENDER_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def load_history() -> dict:
    if HISTORY.exists():
        h = json.loads(HISTORY.read_text(encoding="utf-8"))
    else:
        h = {"note_ids": [], "commenters": {}}
    h.setdefault("probe_fail_count", {})  # 阶段三：{uid: 连续探测失败轮数}
    return h


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


class ProbeSession:
    """单次浏览器会话内完成多批主页探测（作者批/评论者批共用一次 launch 与 client）。

    阶段三(B) 可靠性语义：
      - 成功才落缓存（值含 probed_at）；失败/异常不写 -1，下次运行自动重探
      - 缓存 TTL 30 天；旧 -1 条目视为待重探
      - 单次运行连续 3 次探测异常 → 熔断，本批及后续批次全部跳过（疑似风控，避免烧 cookie）
      - ps.probed/ps.succeeded/ps.failed 记录本会话探测明细（观测/验收/滞留保护用）
    两批都全命中缓存时不启动浏览器。
    """

    def __init__(self) -> None:
        self.cache = load_cache()
        self.probed: list[str] = []
        self.succeeded: set[str] = set()
        self.failed: set[str] = set()
        self.breaker_tripped = False
        self._pw = self._ctx = self._client = None

    async def __aenter__(self) -> "ProbeSession":
        return self

    async def _launch(self) -> None:
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(os.getcwd(), "browser_data", "xhs_user_data_dir"),
            headless=False, viewport={"width": 1920, "height": 1080}, user_agent=UA)
        page = await self._ctx.new_page()
        await page.goto("https://www.xiaohongshu.com")
        cookie_str, cookie_dict = await mc_utils.convert_browser_context_cookies(
            self._ctx, urls=["https://www.xiaohongshu.com"])
        self._client = XiaoHongShuClient(
            headers={"accept": "application/json, text/plain, */*", "user-agent": UA, "Cookie": cookie_str},
            playwright_page=page, cookie_dict=cookie_dict)

    async def probe(self, user_ids: list[str]) -> dict:
        def _stale(entry: dict) -> bool:
            return entry.get("gender") == -1 or time.time() - entry.get("probed_at", 0) > CACHE_TTL_SEC

        todo = [u for u in user_ids if u and (u not in self.cache or _stale(self.cache[u]))]
        print(f"性别/IP 缓存: {len(self.cache)} 已有, {len(todo)} 需探测(含TTL过期/失败重探)")
        if not todo:
            return self.cache
        if self.breaker_tripped:  # 前一批已熔断：后续批次全部跳过，下次运行重试
            print("  ⚠️ 熔断状态，本批探测全部跳过（下次运行自动重试）")
            return self.cache
        if self._client is None:  # 首个真实探测批次才启动浏览器
            await self._launch()
        fails = 0  # 连续异常计数（熔断器）
        try:
            for i, uid in enumerate(todo, 1):
                self.probed.append(uid)  # 记录实际发起的探测（熔断中止者不计）
                entry = None
                try:
                    info = await self._client.get_creator_info(uid)
                    if info and info.get("result", {}).get("success"):
                        b = info.get("basicInfo", {})
                        entry = {"gender": int(b.get("gender") or 0), "ip": b.get("ipLocation", ""),
                                 "nickname": b.get("nickname", ""), "probed_at": int(time.time())}
                    else:
                        print(f"  探测失败 {uid}: 接口 success=False")
                except Exception as e:
                    print(f"  探测失败 {uid}: {type(e).__name__}")
                if entry is None:
                    self.failed.add(uid)
                    fails += 1
                    print(f"  [{i}/{len(todo)}] {uid} -> 失败(不落缓存，下次自动重探)")
                    if fails >= PROBE_BREAKER_N:
                        self.breaker_tripped = True
                        print(f"  ⚠️ 连续 {fails} 次探测异常，熔断中止剩余 {len(todo) - i} 个（疑似风控，继续硬打会烧 cookie）")
                        break
                else:
                    self.cache[uid] = entry
                    self.succeeded.add(uid)
                    self.failed.discard(uid)
                    fails = 0
                    print(f"  [{i}/{len(todo)}] {uid} -> gender={entry['gender']} ip={entry['ip'] or '?'}")
                if i % 10 == 0:
                    save_cache(self.cache)
                await asyncio.sleep(random.uniform(PROBE_MIN_SEC, PROBE_MAX_SEC))  # 5~10s 随机抖动
        finally:
            save_cache(self.cache)
        return self.cache

    async def __aexit__(self, *exc) -> bool:
        save_cache(self.cache)
        if self._ctx is not None:
            await self._ctx.close()
        if self._pw is not None:
            await self._pw.stop()
        return False


def apply_probe_counts(history: dict, ps: "ProbeSession") -> None:
    """消费本会话自上次调用以来的探测增量：成功清除 fail_count、失败 +1（每次失败只计一轮）。

    run_pipeline 在作者批与评论者批后各调用一次；不清空会导致同一 uid 重复累计。
    """
    fc = history.setdefault("probe_fail_count", {})
    for uid in ps.succeeded:
        fc.pop(uid, None)
    for uid in ps.failed:
        fc[uid] = fc.get(uid, 0) + 1
    ps.succeeded.clear()
    ps.failed.clear()


def prefilter(notes: list[dict], seen_notes: set, verdicts: dict) -> tuple[list[dict], list[dict], list[dict], list[dict], dict]:
    """Stage A 免探测预过滤（纯本地数据，零网络）。

    顺序: 台账男→剔除；历史已推送→去重桶(不再判决)；台账女→保留候选(R0 语义：
    不查 IP、优先一切规则)；笔记属地不合规→剔除；其余进入待判决(需主页探测)。
    返回 (verdict_keep, need_probe, dropped, deduped, 免探测计数)。
    """
    verdict_keep, need_probe, dropped, deduped = [], [], [], []
    skip = {"verdict": 0, "seen": 0, "note_ip": 0}
    for r in notes:
        uid = r.get("user_id")
        v = verdicts.get(uid, {}).get("verdict")
        nip = r.get("ip_location") or ""
        if v == "male":
            r["_drop_reason"] = "人工复核确认男生"
            dropped.append(r)
            skip["verdict"] += 1
        elif r["note_id"] in seen_notes:
            deduped.append(r)
            skip["seen"] += 1
        elif v == "female":
            r["_author_gender"] = "人工确认女"
            verdict_keep.append(r)
            skip["verdict"] += 1
        elif nip and not ip_ok(nip):
            r["_drop_reason"] = f"笔记IP={nip}"
            dropped.append(r)
            skip["note_ip"] += 1
        else:
            need_probe.append(r)
    return verdict_keep, need_probe, dropped, deduped, skip


def main() -> None:
    if "--seed" in sys.argv:
        seed_history()
        return
    if "--collect-verdicts" in sys.argv:
        i = sys.argv.index("--collect-verdicts")
        nxt = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
        collect_verdicts(None if nxt is None or nxt.startswith("--") else nxt)
        return
    asyncio.run(run_pipeline())


async def run_pipeline() -> None:
    history = load_history()  # 历史种子仅 --seed 时初始化（阶段三：主流程不再重扫备份文件）
    seen_notes = set(history["note_ids"])

    notes, src = load_notes()
    print(f"笔记来源: {src.name} | 交友关键词唯一笔记: {len(notes)}")

    # Stage A: 免探测预过滤（R4 探测后置——探测只花在幸存帖作者身上）
    verdicts = load_verdicts()
    verdict_keep, need_probe, dropped, deduped, skip = prefilter(notes, seen_notes, verdicts)
    print(f"预过滤免探测: 台账覆盖 {skip['verdict']} / 历史已见 {skip['seen']} / 笔记IP剔除 {skip['note_ip']}"
          f" → {len(need_probe)} 帖待判决")

    kept, reviewed = [], []
    comments_by_note = load_comments()
    order = {r["note_id"]: i for i, r in enumerate(notes)}
    async with ProbeSession() as ps:
        # Stage B: 仅探测幸存帖作者（缓存优先）；Stage C: 规则判决（台账判女优先级最高）
        author_ids = list({r["user_id"] for r in need_probe if r.get("user_id")})
        cache = await ps.probe(author_ids)
        apply_probe_counts(history, ps)
        fail_counts = history["probe_fail_count"]
        for r in need_probe:
            info = cache.get(r.get("user_id"), {})
            g = info.get("gender", -1)
            if g == -1 and fail_counts.get(r.get("user_id"), 0) >= PROBE_FAIL_DROP_N:
                disp, label, basis = "drop", "", "主页多次获取失败(滞留保护)"
            else:
                disp, label, basis = judge_note(r, g, info.get("ip", ""))
            r["_author_gender"] = label if disp == "keep" else ""
            r["_review_hint"] = f"{label}｜依据:{basis}" if disp == "review" else ""
            r["_drop_reason"] = basis if disp == "drop" else ""
            if disp == "drop":
                dropped.append(r)
            elif disp == "review":
                reviewed.append(r)  # 待复核不进历史，未处理会在下次运行再次出现
            else:
                kept.append(r)
        kept = verdict_keep + kept
        print(f"\n规则过滤: 保留 {len(kept)} 新增 + 待复核 {len(reviewed)} + 历史去重 {len(deduped)} + 剔除 {len(dropped)}")

        # 阶段二：女生自荐扫描范围 = 全部合规帖（新增+已推+待复核），旧帖新自荐也能扫出；
        # 已报告过的评论者/人工判男不收录也不再探测（评论者全局只报一次 + 探测预过滤）
        scan_notes = sorted(kept + deduped + reviewed, key=lambda r: order[r["note_id"]])
        reported = history["commenters"]
        cand_ids = []
        for r in scan_notes:
            for c in comments_by_note.get(r["note_id"], []):
                uid = c.get("user_id")
                if not is_female_selfintro(c.get("content") or ""):
                    continue
                if not uid or uid in reported or verdicts.get(uid, {}).get("verdict") == "male":
                    continue
                cand_ids.append(uid)
        ccache = await ps.probe(list(set(cand_ids))) if cand_ids else ps.cache
        apply_probe_counts(history, ps)
    probe_count = len(ps.probed)
    kept.sort(key=lambda r: order[r["note_id"]])  # 各桶恢复输入顺序，输出与旧版可比
    reviewed.sort(key=lambda r: order[r["note_id"]])
    dropped.sort(key=lambda r: order[r["note_id"]])
    deduped.sort(key=lambda r: order[r["note_id"]])
    kept_ids = {r["note_id"] for r in kept}

    intros = []
    reported_now = set(reported)  # R2 评论者全局只报一次：历史已报告 + 本轮已收录（历史在循环后才落盘）
    for r in scan_notes:
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
            if uid in reported_now:
                continue  # R2: 已报告过的评论者（含本轮其他帖已收录）
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
            reported_now.add(uid)

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
        {"统计项": "预过滤免探测(台账/历史/笔记IP)", "数量": skip["verdict"] + skip["seen"] + skip["note_ip"]},
        {"统计项": "本次主页探测", "数量": probe_count},
        {"统计项": "剔除(verdict/IP/性别规则)", "数量": len(dropped)},
        {"统计项": "待复核(男号中性/软信号)", "数量": len(reviewed)},
        {"统计项": "历史已推送(去重)", "数量": len(deduped)},
        {"统计项": "新增输出", "数量": len(kept)},
        {"统计项": "女生自荐(新增评论者)", "数量": len(intros)},
        {"统计项": "自荐来自已推/待复核帖", "数量": sum(1 for x in intros if x["note_id"] not in kept_ids)},
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
