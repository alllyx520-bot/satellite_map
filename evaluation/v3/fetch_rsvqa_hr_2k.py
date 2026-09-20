"""Fetch and materialize the fixed public RSVQA-HR-2k evaluation slice.

The source parquet is public CC-BY-4.0 data.  This script never invents an
answer: question and answer are copied verbatim from the dataset row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = "https://hf-mirror.com/datasets/dmarsili/RSVQA-HR-2k/resolve/main/data/validation-00000-of-00002.parquet"
SOURCE_REPO = "dmarsili/RSVQA-HR-2k"
SOURCE_COMMIT = "bde073e47328d0e1815bd5d3801d0fd4cbcea5a2"
ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw" / "validation-00000-of-00002.parquet"
IMAGES = ROOT / "data" / "images"
MANIFEST = ROOT / "manifest.jsonl"
REFERENCE = ROOT / "reference.jsonl"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_complete_parquet(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 8:
        return False
    with path.open("rb") as handle:
        handle.seek(-4, 2)
        return handle.read() == b"PAR1"


def download() -> None:
    RAW.parent.mkdir(parents=True, exist_ok=True)
    if is_complete_parquet(RAW):
        return
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("缺少 curl，无法下载公开 RSVQA-HR-2k 数据")
    completed = subprocess.run([curl, "-L", "-C", "-", "--connect-timeout", "30", "--max-time", "900", "-o", str(RAW), SOURCE], check=False)
    if completed.returncode or not is_complete_parquet(RAW):
        raise RuntimeError("公开数据下载不完整；没有生成任何评测题或分数")


def materialize(count: int) -> None:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("需要 pyarrow 才能读取公开 parquet；请在隔离 venv 安装后重试") from exc
    table = parquet.ParquetFile(RAW).read_row_group(0).slice(0, count)
    rows = table.to_pylist()
    if len(rows) != count:
        raise RuntimeError(f"公开数据不足 {count} 题；拒绝用模板补齐")
    IMAGES.mkdir(parents=True, exist_ok=True)
    manifest, references = [], []
    for index, row in enumerate(rows):
        image = row.get("image") or {}
        image_bytes = image.get("bytes")
        question, answer = row.get("question"), row.get("answer")
        if not image_bytes or not isinstance(question, str) or not isinstance(answer, str):
            raise RuntimeError(f"源行 {index} 缺少图像、问题或参考答案")
        sample_id = f"rsvqa-hr-2k-r{index:04d}"
        image_name = sample_id + ".png"
        image_path = IMAGES / image_name
        image_path.write_bytes(image_bytes)
        image_hash = sha256(image_path)
        record = {"id": sample_id, "image": f"data/images/{image_name}", "question": question,
                  "source": {"dataset": SOURCE_REPO, "revision": SOURCE_COMMIT, "split": "validation",
                             "parquet_file": RAW.name, "row": index, "license": "CC-BY-4.0"},
                  "image_sha256": image_hash}
        manifest.append(record)
        references.append({"id": sample_id, "answer": answer, "source_answer": answer,
                           "annotation": {"dataset": SOURCE_REPO, "revision": SOURCE_COMMIT,
                                          "split": "validation", "row": index}})
    MANIFEST.write_text("\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in manifest) + "\n", encoding="utf-8")
    REFERENCE.write_text("\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in references) + "\n", encoding="utf-8")
    provenance = {"dataset": SOURCE_REPO, "revision": SOURCE_COMMIT, "license": "CC-BY-4.0", "download_url": SOURCE,
                  "raw_sha256": sha256(RAW), "sample_count": count, "selection": "validation parquet part 0, rows 0-119, in source order",
                  "manifest_sha256": sha256(MANIFEST), "reference_sha256": sha256(REFERENCE)}
    (ROOT / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def materialized_fixture_is_ready() -> bool:
    if not MANIFEST.is_file() or not REFERENCE.is_file() or not (ROOT / "provenance.json").is_file():
        return False
    try:
        rows = [json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line]
        return len(rows) == 120 and all((ROOT / row["image"]).is_file() and sha256(ROOT / row["image"]) == row["image_sha256"] for row in rows)
    except (KeyError, OSError, json.JSONDecodeError):
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--refresh", action="store_true", help="重新下载 parquet 并从源行重建固定题库。")
    args = parser.parse_args()
    if args.count != 120:
        raise SystemExit("V3 固定评测集只能 materialize 120 题")
    if not args.refresh and materialized_fixture_is_ready():
        print(json.dumps({"status": "ready", "samples": args.count, "manifest": str(MANIFEST), "reused": True}, ensure_ascii=False))
    else:
        download()
        materialize(args.count)
        print(json.dumps({"status": "ready", "samples": args.count, "manifest": str(MANIFEST), "reused": False}, ensure_ascii=False))
