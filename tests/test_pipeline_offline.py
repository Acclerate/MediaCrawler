# -*- coding: utf-8 -*-
"""阶段二/三 离线场景测试：合成数据 + FakeClient 注入，不触网、不启动浏览器。

运行: python -m pytest tests/test_pipeline_offline.py -v   （或直接 python tests/test_pipeline_offline.py）

注入方式：FakeSession 继承真实 ProbeSession，仅覆写 _launch 装配 FakeClient（按 uid
返回成功/失败/抛异常），其余探测逻辑（TTL/成功才落缓存/熔断/计数）走真实代码。
覆盖场景：
  阶段三  1. 探测失败不落缓存 + 笔记进待复核而非误杀（test_probe_fail_review_no_cache）
          2. 连续 3 次异常熔断中止，未尝试者不计失败（test_circuit_breaker）
          3. 成功探测清除 fail_count（test_success_clears_fail_count，并入滞留场景）
          4. 连续 3 轮失败滞留保护剔除（test_stranded_drop）
          5. TTL 过期与旧 -1 条目自动重探（test_ttl_and_legacy_reprobe）
  阶段二  6. 已推旧帖的新自荐出现在 Sheet2 并入历史（test_old_note_intro）
          7. 同一评论者多帖自荐只报一次（test_commenter_reported_once，并入场景 6）
          8. 已报告/人工判男的评论者不再探测（test_commenter_probe_prefilter，并入场景 6）
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import process_dating as pmod  # noqa: E402

REAL_SESSION = pmod.ProbeSession  # 真实会话类（FakeSession 的父类）


class FakeClient:
    """get_creator_info 桩：behaviors[uid] = ("ok", gender, ip) | ("nosuccess",) | ("raise",)；缺省成功(性别0/湖北)。"""

    def __init__(self, behaviors: dict, calls: list):
        self.behaviors, self.calls = behaviors, calls

    async def get_creator_info(self, uid: str):
        self.calls.append(uid)
        b = self.behaviors.get(uid)
        if b is None:
            b = ("ok", 0, "湖北")
        if b[0] == "ok":
            return {"result": {"success": True},
                    "basicInfo": {"gender": b[1], "ipLocation": b[2], "nickname": f"昵称_{uid[-4:]}", "genderStr": ""}}
        if b[0] == "nosuccess":
            return {"result": {"success": False}}
        raise RuntimeError("模拟网络异常")


class FakeSession(REAL_SESSION):
    def __init__(self, behaviors: dict, calls: list):
        super().__init__()
        self._behaviors, self._calls = behaviors, calls

    async def _launch(self):
        self._client = FakeClient(self._behaviors, self._calls)


def _note(nid, uid, title, desc, ip="湖北", kw="武汉 交友"):
    return {"note_id": nid, "user_id": uid, "title": title, "desc": desc, "nickname": f"作者_{uid[-4:]}",
            "source_keyword": kw, "time": int(time.time() * 1000), "ip_location": ip,
            "note_url": f"https://www.xiaohongshu.com/explore/{nid}", "liked_count": 1,
            "comment_count": 1, "author_homepage": f"https://www.xiaohongshu.com/user/profile/{uid}"}


def _comment(cid, nid, uid, content, t_off=0):
    return {"comment_id": cid, "note_id": nid, "user_id": uid, "nickname": f"评论者_{uid[-4:]}",
            "content": content, "create_time": int(time.time() * 1000) - t_off, "like_count": 0,
            "author_homepage": f"https://www.xiaohongshu.com/user/profile/{uid}"}


def setup_run(tmp: Path, notes: list, comments: list | None = None, history: dict | None = None,
              cache: dict | None = None, verdicts: dict | None = None, behaviors: dict | None = None):
    """落盘合成输入到 tmp，重定向 process_dating 全部数据路径，注入 FakeSession；返回现场句柄。"""
    (tmp / "jsonl").mkdir(parents=True, exist_ok=True)
    (tmp / "jsonl" / "search_contents_2026-10-16.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in notes), encoding="utf-8")
    (tmp / "jsonl" / "search_comments_2026-10-16.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in (comments or [])), encoding="utf-8")
    hist = {"note_ids": [], "commenters": {}, "probe_fail_count": {}}
    hist.update(history or {})
    (tmp / "history.json").write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    (tmp / "cache.json").write_text(json.dumps(cache or {}, ensure_ascii=False), encoding="utf-8")
    (tmp / "verdicts.json").write_text(json.dumps(verdicts or {}, ensure_ascii=False), encoding="utf-8")

    pmod.GENDER_CACHE = tmp / "cache.json"
    pmod.HISTORY = tmp / "history.json"
    pmod.MV_PATH = tmp / "verdicts.json"
    pmod.JSONL_DIR = tmp / "jsonl"
    pmod.OUT_DIR = tmp
    pmod.PROBE_MIN_SEC = 0  # 测试不等待
    pmod.PROBE_MAX_SEC = 0
    calls: list[str] = []
    beh = behaviors or {}
    pmod.ProbeSession = lambda: FakeSession(beh, calls)
    return {"tmp": tmp, "calls": calls}


def run_pipeline_and_read():
    asyncio.run(pmod.run_pipeline())
    xlsx = sorted(pmod.OUT_DIR.glob("dating_处理结果_*.xlsx"))[-1]
    sheets = {n: xlsx and pd.read_excel(xlsx, sheet_name=n).to_dict(orient="records")
              for n in pd.ExcelFile(xlsx).sheet_names}
    history = json.loads(pmod.HISTORY.read_text(encoding="utf-8"))
    cache = json.loads(pmod.GENDER_CACHE.read_text(encoding="utf-8"))
    return sheets, history, cache


def _find(rows, note_id):
    return next((r for r in rows if r.get("note_id") == note_id), None)


# ---------------------------------------------------------------- 阶段三
def test_probe_fail_review_no_cache(tmp_path):
    """探测失败：不落缓存（无 -1 记录）→ 笔记进待复核(将重试)，fail_count +1；成功者正常保留。"""
    setup_run(tmp_path,
              notes=[_note("n1", "uA", "失败作者的中性帖", "武汉 交友 认识一下"),
                     _note("n2", "uB", "女生主页帖", "找个伴")],
              behaviors={"uA": ("raise",), "uB": ("ok", 1, "湖北")})
    sheets, history, cache = run_pipeline_and_read()

    r1 = _find(sheets["待复核"], "n1")
    assert r1 is not None and "将重试" in str(r1["复核提示"]), f"n1 应进待复核: {r1}"
    assert "uA" not in cache, "失败 uid 不应写入缓存"
    assert history["probe_fail_count"].get("uA") == 1
    assert _find(sheets["新增帖子"], "n2") is not None, "uB(女) 应保留"
    assert cache["uB"]["gender"] == 1 and "probed_at" in cache["uB"]


def test_circuit_breaker(tmp_path):
    """5 个作者全部探测异常：第 3 次连续异常后熔断，仅实际尝试 3 个；未尝试者不计 fail_count。"""
    setup_run(tmp_path,
              notes=[_note(f"n{i}", f"u{i}", f"中性帖{i}", "武汉 交友 认识一下") for i in range(5)],
              behaviors={f"u{i}": ("nosuccess",) for i in range(5)})
    sheets, history, cache = run_pipeline_and_read()

    # 从统计行验证：实际探测数=3；5 帖全部待复核（失败者 fail_count=1<3，未尝试者 fail_count=0）
    stats = {r["统计项"]: r["数量"] for r in sheets["去重与统计"]}
    assert stats["本次主页探测"] == 3, f"熔断后实际探测应为 3: {stats}"
    assert len(sheets["待复核"]) == 5
    assert len(history["probe_fail_count"]) == 3, f"仅实际失败的 3 个 uid 计数: {history['probe_fail_count']}"
    assert cache == {}, "全部失败，缓存不应有任何写入"


def test_stranded_drop_and_success_clears(tmp_path):
    """连续 3 轮失败 → 滞留保护剔除；本轮探测成功者清除历史 fail_count。"""
    setup_run(tmp_path,
              notes=[_note("nE", "uE", "多次失败作者", "武汉 交友 认识一下"),
                     _note("nH", "uH", "曾失败本轮成功", "找个伴")],
              history={"probe_fail_count": {"uE": 2, "uH": 1}},
              behaviors={"uE": ("raise",), "uH": ("ok", 1, "湖北")})
    sheets, history, cache = run_pipeline_and_read()

    drop_rows = [r for r in sheets["剔除明细"] if r["原帖链接"] and "nE" in str(r["原帖链接"])]
    assert drop_rows and "滞留保护" in drop_rows[0]["剔除原因"], f"nE 应滞留保护剔除: {drop_rows}"
    assert _find(sheets["新增帖子"], "nH") is not None, "uH 本轮成功应按规则保留"
    assert history["probe_fail_count"].get("uE") == 3
    assert "uH" not in history["probe_fail_count"], "成功探测应清除计数"


def test_ttl_and_legacy_reprobe(tmp_path):
    """TTL 超 30 天与旧 -1 缓存条目自动重探；旧格式(无 probed_at)迁移补齐。"""
    now = int(time.time())
    setup_run(tmp_path,
              notes=[_note("nT", "uT", "过期缓存作者", "找个伴"),
                     _note("nL", "uL", "男号自称女", "本人女 96年 想认识靠谱的"),
                     _note("nM", "uM", "旧格式缓存作者", "谈个恋爱")],
              cache={"uT": {"gender": 1, "ip": "湖北", "nickname": "t", "probed_at": now - 31 * 86400},
                     "uL": {"gender": -1, "ip": "", "nickname": "l", "probed_at": now},
                     "uM": {"gender": 1, "ip": "湖北", "nickname": "m"}},  # 旧格式：无 probed_at
              behaviors={"uT": ("ok", 1, "湖北"), "uL": ("ok", 2, "湖北"), "uM": None})
    sheets, history, cache = run_pipeline_and_read()

    assert _find(sheets["新增帖子"], "nT") is not None
    r_l = _find(sheets["新增帖子"], "nL")
    assert r_l is not None and r_l["性别判定"] == "男号自称女", f"uL 重探为男号+自称女应保留: {r_l}"
    for uid in ("uT", "uL", "uM"):
        assert cache[uid].get("probed_at", 0) >= now - 60, f"{uid} 缓存应刷新/迁移 probed_at: {cache[uid]}"
    assert cache["uL"]["gender"] == 2, "旧 -1 条目应被成功探测覆盖"


# ---------------------------------------------------------------- 阶段二
def test_old_note_intro(tmp_path):
    """已推旧帖的新自荐进 Sheet2 并入历史；同评论者多帖只报一次；已报告/判男评论者零探测。"""
    setup_run(tmp_path,
              notes=[_note("n_new", "z_new", "新帖", "找个伴"),
                     _note("n_old", "z_old", "已推旧帖", "找个伴")],
              comments=[
                  _comment("c1", "n_old", "x_new", "本人女 98年 坐标武汉 身高165 想认识 →私信"),
                  _comment("c2", "n_new", "y_dup", "女生，99年，坐标汉口，有意者私聊"),
                  _comment("c3", "n_old", "y_dup", "女生，99年，坐标汉口，有意者私聊"),
                  _comment("c4", "n_old", "x_rep", "女生 97年 身高168 认识一下 私信"),
                  _comment("c5", "n_new", "v_male", "小姐姐看看我，认识一下私聊"),
              ],
              history={"note_ids": ["n_old"], "commenters": {"x_rep": "已报告过"}},
              cache={"z_old": {"gender": 1, "ip": "湖北", "nickname": "zo", "probed_at": int(time.time())},
                     "y_dup": {"gender": 1, "ip": "湖北", "nickname": "yd", "probed_at": int(time.time())}},
              verdicts={"v_male": {"verdict": "male", "at": "2026-10-16"}},
              behaviors={"z_new": ("ok", 1, "湖北"), "x_new": ("ok", 1, "湖北")})
    sheets, history, cache = run_pipeline_and_read()

    intros = sheets["女生自荐(新增)"]
    by_uid_home = {str(r["评论者主页"]).rstrip("/").split("/")[-1]: r for r in intros}
    assert "x_new" in by_uid_home, f"旧帖新自荐 x_new 应出现在 Sheet2: {by_uid_home.keys()}"
    assert "已推旧帖" in str(by_uid_home["x_new"]["所属笔记标题"]), "x_new 的自荐应归属已推旧帖"
    assert "x_rep" not in by_uid_home, "已报告过的评论者不应再次出现"
    assert "v_male" not in by_uid_home, "人工判男评论者不应出现"
    y_rows = [r for r in intros if str(r["评论者主页"]).rstrip("/").endswith("y_dup")]
    assert len(y_rows) == 1, f"y_dup 两帖自荐只应报一次: {len(y_rows)}"
    stats = {r["统计项"]: r["数量"] for r in sheets["去重与统计"]}
    assert stats["自荐来自已推/待复核帖"] == 1, f"x_new 来自已推帖应计 1: {stats}"
    assert "x_new" in history["commenters"] and "y_dup" in history["commenters"]
    # 已报告者 x_rep、判男 v_male、已缓存 y_dup 均不应被探测；仅 x_new（与 z_new 作者）触网
    assert set(cache) >= {"z_new", "x_new"} and "x_rep" not in cache and "v_male" not in cache


if __name__ == "__main__":
    import tempfile
    failures = 0
    for fn in (test_probe_fail_review_no_cache, test_circuit_breaker,
               test_stranded_drop_and_success_clears, test_ttl_and_legacy_reprobe,
               test_old_note_intro):
        with tempfile.TemporaryDirectory(prefix=f"t_{fn.__name__}_") as d:
            try:
                fn(Path(d))
                print(f"[PASS] {fn.__name__}")
            except AssertionError as e:
                failures += 1
                print(f"[FAIL] {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
