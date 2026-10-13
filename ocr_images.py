# -*- coding: utf-8 -*-
"""对已爬取的小红书笔记图片做 OCR 文字提取。

用法:
  python ocr_images.py                  # 对 last30d 数据的前 5 篇图文笔记、每篇第 1 张图做演示
  python ocr_images.py 10 3            # 前 10 篇笔记、每篇前 3 张图

输出: data/xhs/jsonl/note_images_ocr.jsonl （note_id, 图片序号, 提取文本, 置信度）
图片缓存目录: data/xhs/ocr_cache/
"""
import io
import json
import sys
import time
from pathlib import Path

import requests
from PIL import Image
import numpy as np
from rapidocr_onnxruntime import RapidOCR

N_NOTES = int(sys.argv[1]) if len(sys.argv) > 1 else 5
N_IMGS = int(sys.argv[2]) if len(sys.argv) > 2 else 1

HEADERS = {
    "Referer": "https://www.xiaohongshu.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
CACHE = Path("data/xhs/ocr_cache")
CACHE.mkdir(parents=True, exist_ok=True)
engine = RapidOCR()


def load_bgr(url: str, note_id: str, idx: int) -> np.ndarray | None:
    """下载图片并转为 BGR ndarray（RapidOCR 兼容 opencv 通道顺序）。"""
    cache_file = CACHE / f"{note_id}_{idx}.webp"
    if not cache_file.exists():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            cache_file.write_bytes(resp.content)
            time.sleep(1)  # 对 CDN 保持礼貌间隔
        except Exception as e:
            print(f"  [download fail] {e}")
            return None
    img = Image.open(io.BytesIO(cache_file.read_bytes())).convert("RGB")
    return np.asarray(img)[:, :, ::-1].copy()  # RGB -> BGR


def main() -> None:
    src = sorted(p for p in Path("data/xhs/jsonl").glob("search_contents_*last30d.jsonl"))[-1]
    recs = [json.loads(l) for l in src.open(encoding="utf-8")]
    targets = [r for r in recs if r.get("image_list")][:N_NOTES]
    print(f"source: {src.name} | notes with images: {len(targets)}")

    out_path = Path("data/xhs/jsonl/note_images_ocr.jsonl")
    with out_path.open("a", encoding="utf-8") as out:
        for rec in targets:
            urls = rec["image_list"].split(",")[:N_IMGS]
            print(f"\n=== {rec['note_id']} | {(rec.get('title') or '')[:30]}")
            for i, url in enumerate(urls):
                img = load_bgr(url, rec["note_id"], i)
                if img is None:
                    continue
                result, _ = engine(img)
                if not result:
                    print(f"  [img {i}] (no text detected)")
                    continue
                lines = [item[1] for item in result]
                conf = sum(item[2] for item in result) / len(result)
                out.write(json.dumps({
                    "note_id": rec["note_id"],
                    "img_index": i,
                    "avg_confidence": round(conf, 3),
                    "text": "\n".join(lines),
                }, ensure_ascii=False) + "\n")
                print(f"  [img {i}] {len(lines)} lines, avg conf {conf:.2f}")
                print("  " + "\n  ".join(lines[:8]))
                if len(lines) > 8:
                    print(f"  ... +{len(lines) - 8} more lines")
    print(f"\nresults appended to: {out_path}")


if __name__ == "__main__":
    main()
