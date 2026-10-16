# -*- coding: utf-8 -*-
"""探测后置整改（R4/§13）的离线 A/B 验收：不触网、不启动浏览器，账号冷却期零风险。

⚠️ 适用范围：本脚本专用于 §15「探测后置」那次整改的基线对比（断言输出逐格一致）。
阶段二/三（2026-10-16 起）引入了有意的行为变更（探测失败→待复核、旧帖自荐扫描等），
之后再运行本脚本断言会失效——阶段二/三的行为验证请用 tests/test_pipeline_offline.py。

用法（主仓根目录执行）:
  python tests/offline_ab_probe_order.py              # 驱动模式：自动建基线 worktree，桩化跑新旧两版并对比
  python tests/offline_ab_probe_order.py --keep       # 保留 worktree 与临时目录供人工检查

子进程模式（由驱动调用，无需手工执行）:
  python tests/offline_ab_probe_order.py --mode new|base --out manifest.json
    环境变量 AB_REPO_ROOT 指向待测代码根（new=当前工作树, base=基线 worktree）。

桩化方式:
  - 新版: ProbeSession 替换为只读缓存桩（记录"本会探测的 uid"，不启动浏览器、不写缓存）
  - 基线: probe_genders 同样替换
  - HISTORY/GENDER_CACHE/OUT_DIR/JSONL_DIR 重定向到临时副本（输入 jsonl 复制进去，输出全部落在临时目录）

对比断言（对应开发计划第 4 步）:
  1. 纯预过滤 uid（台账覆盖/已见帖/笔记IP剔除 且未入缓存）不出现在新版探测清单
  2. 非已见、非台账帖的 keep/review 分桶新旧一致（Sheet1/Sheet3 逐行一致）
  3. 剔除明细：新版是旧版子集，差异仅限"已见帖移出"与"双IP原因文案翻转(主页IP→笔记IP)"
  4. history 与 dating_processed jsonl 运行后一致
  5. 打印新旧探测清单长度差，量化节省比例
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


# ---------------------------------------------------------------- 子进程：桩化执行单版
def run_mode(mode: str, out_path: str) -> None:
    root = Path(os.environ["AB_REPO_ROOT"]).resolve()
    sys.path.insert(0, str(root))
    # 注意: 不 chdir——cwd 固定为主仓根，使 MV_PATH/seed_history 等 cwd 相对路径
    # 在新旧两版中解析到同一份真实数据（基线 worktree 没有 gitignore 的 data/，chdir 会造成环境不对称）
    import pandas as pd  # noqa: E402
    import process_dating as pmod  # noqa: E402

    real_cache = json.loads((REPO / "gender_cache.json").read_text(encoding="utf-8"))

    tmp = Path(tempfile.mkdtemp(prefix=f"ab_{mode}_"))
    (tmp / "jsonl").mkdir()
    for pat in ("search_contents_*.jsonl", "search_comments_*.jsonl"):
        for f in (REPO / "data" / "xhs" / "jsonl").glob(pat):
            shutil.copy(f, tmp / "jsonl" / f.name)
    shutil.copy(REPO / "data" / "xhs" / "dating_seen_history.json", tmp / "history.json")

    pmod.GENDER_CACHE = tmp / "cache.json"  # 桩不读不写它，仅防误触真实缓存
    pmod.HISTORY = tmp / "history.json"
    pmod.JSONL_DIR = tmp / "jsonl"
    pmod.OUT_DIR = tmp

    stub_cache = dict(real_cache)
    if os.environ.get("AB_COLD") == "1":
        # 冷缓存模拟：把输入笔记作者全部视为未缓存 → 两版都需探测，测出真实的探测清单差异
        author_uids = {r.get("user_id") for r in pmod.load_notes()[0]}
        stub_cache = {k: v for k, v in real_cache.items() if k not in author_uids}

    probed_calls: list[list[str]] = []

    if hasattr(pmod, "ProbeSession"):  # 新版：替换会话类
        class StubSession:
            def __init__(self):
                self.cache = dict(stub_cache)
                self.probed: list[str] = []

            async def __aenter__(self):
                return self

            async def probe(self, user_ids):
                todo = [u for u in user_ids if u and u not in self.cache]
                self.probed.extend(todo)
                probed_calls.append(todo)
                print(f"[stub] probe batch: {len(todo)} 需探测")
                return self.cache

            async def __aexit__(self, *exc):
                return False

        pmod.ProbeSession = StubSession
    else:  # 基线版：替换探测函数
        async def stub_probe(user_ids):
            todo = [u for u in user_ids if u and u not in stub_cache]
            probed_calls.append(todo)
            print(f"[stub] probe batch: {len(todo)} 需探测")
            return dict(stub_cache)

        pmod.probe_genders = stub_probe

    pmod.main()

    xlsx = sorted(tmp.glob("dating_处理结果_*.xlsx"))[-1]
    jl = sorted((tmp / "jsonl").glob("dating_processed_*.jsonl"))[-1]
    sheets = {}
    xls = pd.ExcelFile(xlsx)
    for name in xls.sheet_names:
        sheets[name] = json.loads(xls.parse(name).to_json(orient="records", force_ascii=False))
    manifest = {
        "mode": mode,
        "tmp": str(tmp),
        "probed_calls": probed_calls,
        "probed_unique": sorted({u for call in probed_calls for u in call}),
        "sheets": sheets,
        "history_after": json.loads((tmp / "history.json").read_text(encoding="utf-8")),
        "jsonl_out": [json.loads(line) for line in jl.open(encoding="utf-8")],
    }
    Path(out_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{mode}] manifest -> {out_path} | 探测 {len(manifest['probed_unique'])} uid | 输出目录 {tmp}")


# ---------------------------------------------------------------- 驱动：复刻 Stage A 判定"纯预过滤 uid"
def load_input_notes() -> list[dict]:
    """与 process_dating.load_notes 相同口径（独立复刻，供驱动断言用）。"""
    files = [f for f in (REPO / "data" / "xhs" / "jsonl").glob("search_contents_*.jsonl")
             if not re.search(r"_last\d+d", f.name)]
    dated = [f for f in files if re.search(r"search_contents_\d{4}-\d{2}-\d{2}\.jsonl$", f.name)]
    src = max(dated) if dated else max(files)
    latest: dict[str, dict] = {}
    for line in src.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("source_keyword") == "武汉":
            continue
        latest[r["note_id"]] = r
    return list(latest.values())


def exclusively_prefiltered_uids() -> set[str]:
    """作者的**所有**笔记都被 Stage A 免探测分支吃掉 → 该 uid 不应出现在新版探测清单。"""
    sys.path.insert(0, str(REPO))
    from dating_rules import ip_ok

    notes = load_input_notes()
    history = json.loads((REPO / "data" / "xhs" / "dating_seen_history.json").read_text(encoding="utf-8"))
    verdicts = json.loads((REPO / "data" / "xhs" / "manual_verdicts.json").read_text(encoding="utf-8"))
    cache = json.loads((REPO / "gender_cache.json").read_text(encoding="utf-8"))
    seen = set(history["note_ids"])
    uids: set[str] = set()
    for r in notes:
        uid = r.get("user_id")
        if not uid:
            continue
        v = verdicts.get(uid)
        v = v.get("verdict") if isinstance(v, dict) else v  # 兼容新旧台账格式
        nip = r.get("ip_location") or ""
        prefiltered = (v in ("male", "female")) or (r["note_id"] in seen) or (nip and not ip_ok(nip))
        if prefiltered:
            uids.add(uid)
    return {u for u in uids if u not in cache}  # 已缓存者本就不会探测，不参与断言


# ---------------------------------------------------------------- 驱动：对比两份 manifest
def compare(m_old: dict, m_new: dict, forbidden: set[str]) -> bool:
    ok = True

    def fail(msg: str) -> None:
        nonlocal ok
        ok = False
        print(f"  [FAIL] {msg}")

    s1o = {r["note_id"]: r for r in m_old["sheets"]["新增帖子"]}
    s1n = {r["note_id"]: r for r in m_new["sheets"]["新增帖子"]}
    if s1o != s1n:
        fail(f"Sheet1 新增帖子不一致: 仅旧版 {sorted(set(s1o) - set(s1n))[:5]} / 仅新版 {sorted(set(s1n) - set(s1o))[:5]}")
        for k in sorted(set(s1o) & set(s1n)):
            if s1o[k] != s1n[k]:
                fail(f"  Sheet1 行差异 note_id={k}: 旧={s1o[k]} 新={s1n[k]}")
                break
    else:
        print(f"  [PASS] Sheet1 新增帖子 逐行一致 ({len(s1n)} 行)")

    s3o = {r["note_id"]: r for r in m_old["sheets"]["待复核"]}
    s3n = {r["note_id"]: r for r in m_new["sheets"]["待复核"]}
    if s3o != s3n:
        fail(f"Sheet3 待复核不一致: 仅旧版 {sorted(set(s3o) - set(s3n))[:5]} / 仅新版 {sorted(set(s3n) - set(s3o))[:5]}")
    else:
        print(f"  [PASS] Sheet3 待复核 逐行一致 ({len(s3n)} 行)")

    if m_old["sheets"]["女生自荐(新增)"] != m_new["sheets"]["女生自荐(新增)"]:
        fail("Sheet2 女生自荐(新增) 不一致")
    else:
        print(f"  [PASS] Sheet2 女生自荐(新增) 逐行一致 ({len(m_new['sheets']['女生自荐(新增)'])} 行)")

    # Sheet4：新版 ⊆ 旧版；差异仅允许"已见帖移出"与"主页IP→笔记IP 文案翻转"
    hist_in = set(json.loads((REPO / "data" / "xhs" / "dating_seen_history.json").read_text(encoding="utf-8"))["note_ids"])
    for seed in ("backup_组合关键词_2026-10-12.jsonl", "dating_processed_2026-10-13.jsonl"):  # seed_history 的两个种子文件
        p = REPO / "data" / "xhs" / "jsonl" / seed
        if p.exists():
            for line in p.open(encoding="utf-8"):
                hist_in.add(json.loads(line)["note_id"])
    by_link = lambda rows: {r["原帖链接"]: r for r in rows}  # noqa: E731
    o4, n4 = by_link(m_old["sheets"]["剔除明细"]), by_link(m_new["sheets"]["剔除明细"])
    if set(n4) - set(o4):
        fail(f"Sheet4 出现旧版没有的剔除行: {sorted(set(n4) - set(o4))[:5]}")
    moved, flipped = [], []
    for link in set(o4) - set(n4):
        # 旧版独有行：其 note_id 须能对应已见帖（允许移出）——剥掉链接查询串后取末段 note_id
        nid = link.split("?")[0].rstrip("/").split("/")[-1]
        if nid in hist_in:
            moved.append(nid)
        else:
            fail(f"Sheet4 旧版独有剔除行但非已见帖（不应消失）: {link}")
    for link, r in n4.items():
        ro = o4.get(link)
        if ro and ro["剔除原因"] != r["剔除原因"]:
            if ro["剔除原因"].startswith("主页IP") and r["剔除原因"].startswith("笔记IP"):
                flipped.append(link)
            else:
                fail(f"Sheet4 剔除原因意外变化 {link}: 旧={ro['剔除原因']} 新={r['剔除原因']}")
    print(f"  [PASS] Sheet4 子集关系成立（已见帖移出 {len(moved)} 行，双IP原因翻转 {len(flipped)} 行）"
          if ok else "  Sheet4 存在违规差异，见上")

    if m_old["history_after"]["note_ids"] != m_new["history_after"]["note_ids"] or \
            m_old["history_after"]["commenters"] != m_new["history_after"]["commenters"]:
        fail("history 运行后不一致")
    else:
        print(f"  [PASS] dating_seen_history 运行后一致 (累计 {len(m_new['history_after']['note_ids'])} 帖)")

    ko = [{k: v for k, v in r.items()} for r in m_old["jsonl_out"]]
    kn = [{k: v for k, v in r.items()} for r in m_new["jsonl_out"]]
    if [r["note_id"] for r in ko] != [r["note_id"] for r in kn] or ko != kn:
        fail("dating_processed jsonl 不一致（note_id 序列或字段内容）")
    else:
        print(f"  [PASS] dating_processed jsonl 一致 ({len(kn)} 行)")

    po, pn = set(m_old["probed_unique"]), set(m_new["probed_unique"])
    if not pn <= po:
        fail(f"新版探测了旧版没探的 uid: {sorted(pn - po)[:5]}")
    bad = pn & forbidden
    if bad:
        fail(f"纯预过滤 uid 仍被探测: {sorted(bad)[:5]}")
    else:
        print(f"  [PASS] 纯预过滤 uid（{len(forbidden)} 个）零探测；新版 ⊆ 旧版探测清单")
    saved = len(po) - len(pn)
    print(f"  探测量: 旧版 {len(po)} → 新版 {len(pn)}（省 {saved}，{saved * 100 // max(len(po), 1)}%）"
          f" | 旧版清单={m_old['tmp']} 新版清单={m_new['tmp']}")
    return ok


# ---------------------------------------------------------------- 驱动入口
def run_pair(tag: str, wt: Path, tmp: Path, cold: bool) -> tuple[dict, dict]:
    """跑 base/new 两个子进程（cwd=REPO 保证相对路径环境一致），返回 (旧manifest, 新manifest)。"""
    out: dict[str, dict] = {}
    for m, root in (("base", wt), ("new", REPO)):
        mf = tmp / f"manifest_{tag}_{m}.json"
        env = {**os.environ, "AB_REPO_ROOT": str(root), "PYTHONIOENCODING": "utf-8"}
        if cold:
            env["AB_COLD"] = "1"
        r = subprocess.run([PY, __file__, "--mode", m, "--out", str(mf)],
                           cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8")
        tail = "\n".join((r.stdout or "").splitlines()[-3:])
        print(f"--- [{tag}/{m}] {tail}")
        if r.returncode != 0:
            print(f"[{tag}/{m}] 运行失败:\n{r.stderr[-3000:]}")
            sys.exit(2)
        out[m] = json.loads(mf.read_text(encoding="utf-8"))
    return out["base"], out["new"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["new", "base"])
    ap.add_argument("--out")
    ap.add_argument("--keep", action="store_true", help="保留基线 worktree 与临时目录")
    args = ap.parse_args()
    if args.mode:
        run_mode(args.mode, args.out)
        return

    # 已跟踪文件有改动 → 基线=HEAD（改动未提交）；否则基线=上一提交。未跟踪文件不算（如 .zcode/ 会话目录）
    st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                        cwd=REPO, capture_output=True, text=True).stdout.strip()
    dirty = bool(st)
    base_ref = "HEAD" if dirty else "HEAD^"
    wt = REPO.parent / "MediaCrawler_ab_base"
    print(f"基线: {base_ref}（工作树{'含未提交改动' if dirty else '已提交'}）")
    if wt.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO, check=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(wt), base_ref], cwd=REPO, check=True)

    tmp = Path(tempfile.mkdtemp(prefix="ab_driver_"))
    m_old, m_new = run_pair("hot", wt, tmp, cold=False)  # 热缓存：真实缓存下的输出一致性
    print("\n=== A/B 对比（热缓存·真实数据·输出一致性） ===")
    ok = compare(m_old, m_new, exclusively_prefiltered_uids())

    print("\n=== A/B 对比（冷缓存·探测清单测量） ===")
    c_old, c_new = run_pair("cold", wt, tmp, cold=True)  # 冷缓存：量化探测节省
    po, pn = set(c_old["probed_unique"]), set(c_new["probed_unique"])
    if not pn <= po:
        ok = False
        print(f"  [FAIL] 冷缓存下新版探测了旧版没探的 uid: {sorted(pn - po)[:5]}")
    bad = pn & exclusively_prefiltered_uids()
    if bad:
        ok = False
        print(f"  [FAIL] 冷缓存下纯预过滤 uid 仍被探测: {sorted(bad)[:5]}")
    if ok:
        saved = len(po) - len(pn)
        print(f"  [PASS] 冷缓存探测量: 旧版(先探测后过滤) {len(po)} → 新版(仅幸存作者) {len(pn)}"
              f"（省 {saved} 个，{saved * 100 // max(len(po), 1)}%）")

    if not args.keep:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO)
        for mf in tmp.glob("manifest_*.json"):
            d = Path(json.loads(mf.read_text(encoding="utf-8"))["tmp"])
            shutil.rmtree(d, ignore_errors=True)
        print("(worktree 与临时目录已清理，--keep 可保留)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
