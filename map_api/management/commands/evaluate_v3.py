"""Real public VQA comparison and a separately-labelled V3 harness smoke."""
from __future__ import annotations
import base64, hashlib, json, time, uuid
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from evaluation.v3.scoring import aggregate, read_jsonl, score_one, promotion
from map_api.v3.provider import ProviderFailure, tool_call

ROOT = Path(__file__).resolve().parents[3] / "evaluation" / "v3"
DIRECT_PROMPT = "Answer the visual question using only the supplied remote-sensing image. Reply with only the answer, with no explanation."

class Command(BaseCommand):
    help = "真实 RSVQA 120 题 direct-vision 对照；另提供真实 V3 harness 小样本 smoke。"
    def add_arguments(self, p):
        p.add_argument("--validate", action="store_true"); p.add_argument("--run", action="store_true", help="实际运行 3×120 direct-vision 调用。")
        p.add_argument("--harness-smoke", action="store_true", help="在隔离 Django test SQLite 中运行一个真实 harness 样本。")
        p.add_argument("--harness-samples", type=int, default=0, help="真实 harness 评测样本数（逐样本独立 test DB/媒体目录）。")
        p.add_argument("--output", default=str(ROOT / "results" / "latest")); p.add_argument("--retries", type=int, default=2); p.add_argument("--variant", choices=["baseline","candidate-a","candidate-b"])
        p.add_argument("--candidate-a", default="qwen3.8-max-0902"); p.add_argument("--candidate-b", default="qwen3.7-plus")
        p.add_argument("--summarize", action="store_true", help="只汇总已落盘结果，不调用模型")
    def handle(self, *args, **opts):
        samples, refs = self._load_and_validate()
        if not 0 <= opts["retries"] <= 3: raise CommandError("--retries 必须在 0 至 3")
        harness_count=max(int(opts["harness_samples"]),1 if opts["harness_smoke"] else 0)
        if harness_count:
            if harness_count > len(samples): raise CommandError("harness 样本数超过固定题库")
            for sample in samples[:harness_count]: self._harness_smoke(sample, opts["output"])
        if opts["validate"] or not (opts["run"] or opts["summarize"]):
            self.stdout.write(json.dumps({"status":"ready","samples":len(samples),"direct_models":3,"live_model_called":bool(harness_count)},ensure_ascii=False)); return
        output=Path(opts["output"]).resolve(); output.mkdir(parents=True,exist_ok=True)
        variants={"baseline":("direct-vision-baseline","qwen3-vl-plus"),"candidate-a":("direct-vision-candidate-a",opts["candidate_a"]),"candidate-b":("direct-vision-candidate-b",opts["candidate_b"])}
        selected=[opts["variant"]] if opts["variant"] else list(variants)
        failures = {}
        if not opts["summarize"]:
            for key in selected:
                name,model=variants[key]
                try:
                    self._run_direct(name,model,samples,refs,output,opts["retries"])
                except CommandError as exc:
                    failures[name] = str(exc)
        summaries={}
        incomplete={}
        for key,(name,_) in variants.items():
            path=output/f"{name}.jsonl"; rows=read_jsonl(path) if path.exists() else []
            if len({row['id'] for row in rows}) != len(rows) or set(row['id'] for row in rows)-set(refs):
                raise CommandError("结果日志含重复或不属于当前题库的记录")
            if len(rows)==120: summaries[name]=aggregate([row["metrics"] for row in rows])
            else: incomplete[name]={"completed_samples":len(rows),"required_samples":120,"status":"incomplete"}
        baseline=summaries.get("direct-vision-baseline")
        report={"status":"completed" if not incomplete else "incomplete","suite":"direct-vision-identical-input","samples":120,"variants":summaries,"incomplete":incomplete,"failures":failures,"promotion":{n:promotion(baseline,summaries[n]) for n in summaries if n!="direct-vision-baseline"} if baseline else {},"reference_answers_exported":False,"note":"三组仅比较视觉模型；这不是 V3 harness 对照。harness 另以 --harness-smoke 真实执行。"}
        (output / "summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); self.stdout.write(json.dumps(report,ensure_ascii=False,indent=2))
        if failures:
            raise CommandError("部分模型请求失败；已保存真实进度，完整评测尚未通过")
    def _load_and_validate(self):
        manifest,reference,provenance=ROOT/"manifest.jsonl",ROOT/"reference.jsonl",ROOT/"provenance.json"
        if not all(p.is_file() for p in (manifest,reference,provenance)): raise CommandError("真实 RSVQA 题库未 materialize；拒绝用模板评分")
        samples,refs=read_jsonl(manifest),{x["id"]:x for x in read_jsonl(reference)}
        if len(samples)!=120 or len(refs)!=120 or set(x["id"] for x in samples)!=set(refs): raise CommandError("固定 120 题或原始参考标注不完整；拒绝评分")
        tasks=json.loads((ROOT/"long_tasks.json").read_text(encoding="utf-8"))
        if len(tasks)!=20 or any(x.get("status") not in {"not_run","completed","failed","incomplete"} for x in tasks): raise CommandError("20 个长任务状态无效")
        for s in samples:
            image=ROOT/s["image"]
            if not image.is_file() or self._hash(image)!=s.get("image_sha256"): raise CommandError(f"真实影像缺失或损坏: {s['id']}；拒绝评分")
        return samples,refs
    def _run_direct(self, variant, model, samples, refs, output, retries):
        path=output/f"{variant}.jsonl"; prior=read_jsonl(path) if path.exists() else []; rows={x["id"]:x for x in prior}
        if len(rows)!=len(prior): raise CommandError(f"{path} 含重复结果，拒绝汇总")
        if any(row.get("model") != model or row.get("variant") != variant for row in prior):
            raise CommandError("结果目录中的模型与本次配置不同，请使用新的输出目录")
        with path.open("a",encoding="utf-8") as handle:
            for sample in samples:
                if sample["id"] in rows: continue
                response,failures=self._call_with_retry(model,sample,retries)
                if response is None:
                    with (output/f"{variant}.failures.jsonl").open("a",encoding="utf-8") as failed:
                        failed.write(json.dumps({"id":sample["id"],"variant":variant,"model":model,"attempt_failures":failures},ensure_ascii=False,sort_keys=True)+"\n")
                    raise CommandError(f"{variant}/{sample['id']} 在 {retries+1} 次请求后仍失败: {failures[-1]}；已保留逐题日志，未生成总分")
                scored=score_one(sample,refs[sample["id"]],response["content"])
                row={"id":sample["id"],"variant":variant,"model":response["model"],"prediction":scored.pop("prediction"),"metrics":scored,"latency_ms":response["latency_ms"],"usage":response["usage"],"attempt_failures":failures}
                handle.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n"); handle.flush(); rows[sample["id"]]=row
        return [rows[s["id"]]["metrics"] for s in samples]
    def _call_with_retry(self,model,sample,retries):
        failures=[]
        for attempt in range(retries+1):
            try: return self._direct_call(model,sample),failures
            except ProviderFailure as exc:
                failures.append({"attempt":attempt+1,"code":exc.code,"retryable":exc.retryable})
                if not exc.retryable or attempt==retries: break
                time.sleep(min(8,2**attempt))
        return None,failures
    def _direct_call(self,model,sample):
        encoded="data:image/png;base64,"+base64.b64encode((ROOT/sample["image"]).read_bytes()).decode("ascii")
        return tool_call([{"role":"system","content":DIRECT_PROMPT},{"role":"user","content":sample["question"]}],[],role="vision",images=[encoded],timeout=120,model=model)
    def _harness_smoke(self,sample,output):
        import io, tempfile
        from django.test import override_settings
        from django.test.utils import setup_databases,teardown_databases
        old=setup_databases(verbosity=0,interactive=False,keepdb=False)
        try:
            with tempfile.TemporaryDirectory(prefix="v3-evaluation-media-") as media, override_settings(MEDIA_ROOT=media):
                from map_api.models import Conversation
                from map_api.v3 import assets
                from map_api.v3.conversations import submit
                from map_api.v3.harness import execute_run
                owner="evaluation-"+uuid.uuid4().hex; image=ROOT/sample["image"]; raw=image.read_bytes()
                conversation=Conversation.objects.create(owner_session_key=owner,title="V3 evaluation harness smoke")
                upload=assets.create_upload(owner,image.name,len(raw),conversation=conversation)
                assets.upload_chunk(upload.id,owner,0,io.BytesIO(raw)); attachment=assets.complete_upload(upload.id,owner)
                attachment=assets.process_attachment(attachment.id)
                if attachment.status!="ready": raise CommandError("harness smoke 附件未完成真实导入")
                _,run=submit(str(conversation.id),owner,{"content":sample["question"],"attachment_ids":[str(attachment.id)],"attachment_names":{str(attachment.id):attachment.name},"references":[],"request_id":"evaluation-smoke-"+uuid.uuid4().hex,"delivery":"queue"})
                execute_run(run.id); run.refresh_from_db()
                final=run.conversation_messages.filter(role="assistant").order_by("sequence").last()
                failed_tools = [{"name": row.name, "error": row.result.get("error") or row.error}
                                for row in run.tool_calls.all() if row.result.get("error") or row.error]
                report={"status":run.status,"run_error":run.error,"run_id":str(run.id),"engine":run.execution_engine,"turns":run.turns.count(),"tool_calls":run.tool_calls.count(),"failed_tools":failed_tools,"usage":run.usage,"final_answer":final.content if final else None,"final_references":final.parts if final else [],"temporary_test_database":True,"temporary_media_root":True,"coverage_limit":"一个真实来源样本；不代表 120 题 harness 成绩。"}
            destination=Path(output).resolve(); destination.mkdir(parents=True,exist_ok=True); (destination/f"harness-{sample['id']}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            if run.execution_engine!="harness": raise CommandError("harness smoke 未实际使用 harness execution engine")
            self.stdout.write(json.dumps({"harness_smoke":report},ensure_ascii=False))
        finally: teardown_databases(old,verbosity=0)
    @staticmethod
    def _hash(path):
        digest=hashlib.sha256()
        with Path(path).open("rb") as h:
            for c in iter(lambda:h.read(1024*1024),b""): digest.update(c)
        return digest.hexdigest()
