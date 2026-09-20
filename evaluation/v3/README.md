# V3 可复现公开评测包

固定集是 `dmarsili/RSVQA-HR-2k` 的 validation parquet 第一部分前 120 行，原始问题、图像和答案均来自该公开 CC-BY-4.0 数据集（原始 RSVQA-HR validation 子集），不包含合成真值或模板补题。首次执行：

```powershell
.\.codex-runtime\venv\Scripts\python.exe evaluation\v3\fetch_rsvqa_hr_2k.py
.\.codex-runtime\venv\Scripts\python.exe manage.py evaluate_v3 --validate
```

真实模型运行须显式添加 `--run`；命令经 `map_api.v3.provider.tool_call` 调用应用配置的视觉模型，不读取或打印凭据。每个样本原子写入，可用相同 `--output` 恢复。公开题库没有定位标注和多轮标注，相关指标严格标为 unavailable/not_run，不能作为晋升的虚假通过依据。`--variant candidate-b` 可独立恢复该变体，不会被另一个变体的失败阻塞。

`long_tasks.json` 是 20 个真实数据任务协议，初始为 `not_run`，后续可记录 `completed`、`failed` 或 `incomplete`，不计入 RSVQA 分数。direct-vision 的 `answer_exact` 只表示候选信号；因定位和任务完成真值缺失，晋升门禁始终为 `false`。

真实 harness 不是 prompt 对照：`--harness-samples N` 会对固定前 N 个样本逐个创建隔离 test SQLite 和临时媒体目录，并实际运行上传、处理、Conversation、submit 与 execute_run。每个样本单独保存状态、工具失败、usage、最终回答和引用；它不产生未具备真值条件的晋升分数。

## 核对（2026-09-14）

- `manage.py evaluate_v3` 的实际参数：`--validate`、`--run`、`--harness-smoke`、`--harness-samples N`、`--output`、`--retries`、`--variant {baseline,candidate-a,candidate-b}`、`--candidate-a`、`--candidate-b`、`--summarize`。本文上述用法与代码一致。
- 固定集规模已核对：`manifest.jsonl` 与 `reference.jsonl` 各 **120** 行。
- 视觉基线仍为 `qwen3-vl-plus`；候选模型（`--candidate-a qwen3.8-max-0902`、`--candidate-b qwen3.7-plus`）**未晋升**——定位与多轮真值缺失时晋升门禁恒为 `false`，`long_tasks.json` 仍未计入 RSVQA 分数。
