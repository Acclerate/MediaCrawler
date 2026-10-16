# -*- coding: utf-8 -*-
"""阶段五(C3)：从历史输出半自动构建黄金标注集草稿（不触网）。

用法: python tests/build_golden_draft.py [--out tests/dating_golden]
产物（gitignored，含真实爬取内容，不入库）:
  labeled_notes.jsonl    {note_id,title,desc,nickname,gender,home_ip,note_ip,expected,label_expected?}
  labeled_comments.jsonl {comment_id,content,expected}
  _meta.json             构建信息（来源、数量、剔除统计）

标注来源与规则:
  - 遍历全部 dating_处理结果_*.xlsx（按文件名时间排序，后运行的结论覆盖先前的）:
      新增帖子→keep(标签取"性别判定"列) / 待复核→review / 剔除明细→drop
  - 语义变换: 剔除原因="主页获取失败" 的行 → review（阶段三起探测失败不再误杀）
  - 性别/主页IP 取 gender_cache.json（按 user_id）；笔记原始文本从 jsonl 目录反查
  - 自校验: 逐条用当前 dating_rules 回放，回放不一致的条目剔除并计入 _meta（草稿需人工抽查，
    尤其 review 档；人工改判后直接编辑 jsonl，本脚本加 --force 才会覆盖）
"""
import argparse
import glob
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import dating_rules  # noqa: E402

JSONL_DIR = REPO / "data" / "xhs" / "jsonl"
OUT_DIR = REPO / "data" / "xhs"
CACHE = REPO / "gender_cache.json"


def load_note_records() -> dict[str, dict]:
    """note_id → 原始笔记记录（title/desc/nickname/ip_location/user_id），jsonl 目录全量反查。"""
    idx: dict[str, dict] = {}
    for fp in sorted(JSONL_DIR.glob("*.jsonl")):
        for line in fp.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("note_id") and r.get("desc") is not None and "title" in r:
                idx[r["note_id"]] = r  # 后读的文件覆盖（更完整）
    return idx


def load_expected_from_xlsx() -> dict[str, dict]:
    """note_id → {expected, label}。后运行的输出覆盖先前的；旧"主页获取失败"剔除映射为 review。"""
    exp: dict[str, dict] = {}
    files = sorted(glob.glob(str(OUT_DIR / "dating_处理结果_*.xlsx")))
    for fp in files:
        xls = pd.ExcelFile(fp)
        names = set(xls.sheet_names)

        def rows(sheet):
            return pd.read_excel(xls, sheet_name=sheet).to_dict(orient="records") if sheet in names else []

        for row in rows("新增帖子"):
            if row.get("note_id"):
                exp[str(row["note_id"])] = {"expected": "keep", "label": str(row.get("性别判定") or "")}
        for row in rows("待复核"):
            if row.get("note_id"):
                exp[str(row["note_id"])] = {"expected": "review", "label": ""}
        for row in rows("剔除明细"):
            link = str(row.get("原帖链接") or "")
            nid = link.split("?")[0].rstrip("/").split("/")[-1]
            if not nid:
                continue
            reason = str(row.get("剔除原因") or "")
            if reason == "主页获取失败":  # 阶段三语义变换：探测失败 → 待复核重试
                exp[nid] = {"expected": "review", "label": ""}
            else:
                exp[nid] = {"expected": "drop", "label": ""}
    return exp


def build_notes(records: dict, expected: dict, cache: dict) -> tuple[list[dict], list[dict]]:
    out, excluded = [], []
    for nid, exp in expected.items():
        r = records.get(nid)
        if not r or not r.get("desc"):
            excluded.append({"note_id": nid, "reason": "原始笔记缺失(无desc)"})
            continue
        uid = r.get("user_id")
        c = cache.get(uid) or {}
        g = int(c.get("gender", -1))
        home_ip = str(c.get("ip", "") or "")
        rec = {"note_id": nid, "title": r.get("title") or "", "desc": r.get("desc") or "",
               "nickname": r.get("nickname") or "", "gender": g, "home_ip": home_ip,
               "note_ip": r.get("ip_location") or "", "expected": exp["expected"]}
        if exp["expected"] == "keep" and exp["label"]:
            rec["label_expected"] = exp["label"]
        disp, label, _ = dating_rules.judge_note(rec, g, home_ip)
        if disp != rec["expected"] or (rec.get("label_expected") and label != rec["label_expected"]):
            excluded.append({"note_id": nid, "reason": f"回放不一致: 期望{rec['expected']}/{rec.get('label_expected','')}"
                                                      f" 实际{disp}/{label}"})
            continue
        out.append(rec)
    return out, excluded


def build_comments() -> tuple[list[dict], int]:
    by_id: dict[str, str] = {}
    for fp in sorted(JSONL_DIR.glob("search_comments_*.jsonl")):
        for line in fp.open(encoding="utf-8"):
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue
            if c.get("comment_id"):
                by_id[str(c["comment_id"])] = c.get("content") or ""
    pos = [{"comment_id": cid, "content": t, "expected": True}
           for cid, t in by_id.items() if dating_rules.is_female_selfintro(t)]
    neg_pool = [{"comment_id": cid, "content": t, "expected": False}
                for cid, t in by_id.items() if not dating_rules.is_female_selfintro(t)]
    rng = random.Random(42)
    neg = rng.sample(neg_pool, min(len(neg_pool), max(len(pos), 30)))
    return sorted(pos + neg, key=lambda x: x["comment_id"]), len(by_id)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent / "dating_golden"))
    ap.add_argument("--force", action="store_true", help="覆盖已存在的人工修订版")
    args = ap.parse_args()
    out_dir = Path(args.out)
    if out_dir.exists() and not args.force:
        print(f"{out_dir} 已存在（可能含人工修订），加 --force 才覆盖")
        return

    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    records = load_note_records()
    expected = load_expected_from_xlsx()
    notes, excluded = build_notes(records, expected, cache)
    comments, total_comments = build_comments()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "labeled_notes.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in notes), encoding="utf-8")
    (out_dir / "labeled_comments.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in comments), encoding="utf-8")
    dist = {}
    for r in notes:
        dist[r["expected"]] = dist.get(r["expected"], 0) + 1
    (out_dir / "_meta.json").write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "draft（review 档与边界条目建议人工抽查后定稿；定稿后勿再运行构建器覆盖）",
        "sources": [Path(f).name for f in sorted(glob.glob(str(OUT_DIR / "dating_处理结果_*.xlsx")))],
        "notes_total": len(notes), "notes_dist": dist,
        "notes_excluded": excluded,
        "comments_total_pool": total_comments,
        "comments_true": sum(1 for c in comments if c["expected"]),
        "comments_false": sum(1 for c in comments if not c["expected"]),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"黄金集草稿 → {out_dir}")
    print(f"  笔记 {len(notes)} 条（分布 {dist}），回放不一致剔除 {len(excluded)} 条（见 _meta.json）")
    print(f"  评论 {len(comments)} 条（正例 {sum(1 for c in comments if c['expected'])} / "
          f"负例 {sum(1 for c in comments if not c['expected'])}，池 {total_comments}）")


if __name__ == "__main__":
    main()
