"""Deterministic scoring; source annotations are the only answer truth."""
from __future__ import annotations
import json, re, unicodedata
from pathlib import Path
def read_jsonl(path: Path): return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
def normalize(value): return re.sub(r"[\s\.,;:!?，。；：！？]+", "", unicodedata.normalize("NFKC",str(value)).casefold().strip())
def parse_response(text):
    return text.strip()
def score_one(sample, reference, output):
    prediction=parse_response(output); return {"id":sample["id"],"answer_exact":normalize(prediction)==normalize(reference["answer"]),"prediction":prediction,"source_row":sample["source"]["row"]}
def aggregate(rows):
    if not rows: raise ValueError("没有可计分的真实题目")
    count=len(rows)
    return {"scored":count,"answer_exact":round(sum(x["answer_exact"] for x in rows)/count*100,2),"localization":{"status":"unavailable","reason":"RSVQA-HR-2k 行不含对象坐标；不伪造定位真值"},"task_completion":{"status":"unavailable","reason":"固定公开 VQA 没有真实长任务完成标注"},"multi_turn_reference":{"status":"not_run","reason":"固定公开 VQA 是单轮标注；长任务协议另列且不计分"}}
def promotion(current,candidate):
    gain=candidate["answer_exact"]-current["answer_exact"]
    return {"eligible":False,"answer_exact_gain":round(gain,2),"required_answer_exact_gain":5.0,"key_metrics_non_regressing":False,"rule":"VQA answer_exact 仅为候选信号。缺少真实定位和任务完成标注时，晋升门禁必须为 false。"}
