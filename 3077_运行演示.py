# SPDX-License-Identifier: MIT
"""真实本地HTTP演练；不会连接真实MES。"""
from __future__ import annotations
import argparse
from collections import Counter
import copy
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

HERE=Path(__file__).resolve().parent

def module(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    obj=importlib.util.module_from_spec(spec);sys.modules[name]=obj;spec.loader.exec_module(obj)
    return obj


def preview(model,plan,journal,replay,report):
    esc=lambda value:html.escape(str(value),quote=True)
    labels={"create":"新增","update":"更新","unchanged":"一致"}
    writes={w["key"]:w for w in journal["writes"]}
    rows=[]
    for operation in plan["operations"]:
        p=operation["payload"];pages=", ".join(map(str,p["source_pages"]))
        result="已读取核实" if writes.get(operation["key"],{}).get("verified") else "未写入"
        rows.append(f'<tr><td>{esc(p["step_id"])}</td><td>{esc(p["title"])}</td><td>{esc(pages)}</td><td class="{operation["action"]}">{labels[operation["action"]]}</td><td>{result}</td><td>重跑一致</td></tr>')
    cards=[("提取步骤",report["extracted_steps"]),("计划新增",report["planned_create"]),("计划更新",report["planned_update"]),("原已一致",report["planned_unchanged"]),("实际核实写入",report["verified_writes"]),("重跑新增写入",report["rerun_write_requests"])]
    card_html="".join(f'<div class="card"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>' for label,value in cards)
    return '''<!doctype html><html lang="zh-CN" translate="no"><meta charset="utf-8"><meta name="google" content="notranslate"><meta name="viewport" content="width=device-width,initial-scale=1"><title>3077 作业步骤提取与模拟同步</title><style>
*{box-sizing:border-box}body{margin:0;background:#f0f4f8;color:#183047;font:14px "Microsoft YaHei",Arial,sans-serif}main{max-width:1240px;margin:auto;padding:26px 28px}.top{display:flex;align-items:center;justify-content:space-between;color:#55718c;font-weight:600;font-size:12px;letter-spacing:1px}h1{font-size:28px;margin:12px 0 8px;line-height:1.3}p{margin:0;color:#61778c;line-height:1.7}.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:20px 0 16px}.card{background:white;border:1px solid #dfe8f0;border-radius:9px;padding:12px 16px}.card span{display:block;color:#6c7f91;font-size:12px}.card strong{display:block;font-size:27px;line-height:1.4;color:#214d72}.panel{background:#fff;border:1px solid #dfe8f0;border-radius:10px;overflow:hidden}.bar{display:flex;justify-content:space-between;padding:12px 16px;font-weight:600;border-bottom:1px solid #e6edf3}.bar span{font-size:12px;color:#66829a}.scroll{max-height:306px;overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}thead{background:#eaf1f7;color:#536e87;position:sticky;top:0}th{text-align:left;padding:11px 15px;font-size:12px;font-weight:600}td{padding:11px 15px;border-bottom:1px solid #eef2f5}td:first-child{font-family:Consolas,monospace;color:#486780}td:nth-child(2){width:42%}.create{color:#20785e}.update{color:#966a13}.unchanged{color:#65798d}.foot{display:flex;gap:20px;justify-content:space-between;margin-top:13px;font-size:12px;line-height:1.7;color:#677e92}.hash{font:10px Consolas,monospace;word-break:break-all;margin-top:6px;color:#7890a5}@media(max-width:760px){main{padding:18px}.cards{grid-template-columns:repeat(3,1fr)}h1{font-size:23px}.bar,.foot{display:block}.scroll{max-height:330px}td,th{padding:10px 8px}td:nth-child(2){width:auto}}
</style><main><div class="top"><span>3077 / 独立演示</span><span>文字 PDF → JSON → 本地模拟 HTTP</span></div><h1>作业步骤提取与同步核对</h1><p>自造训练文档 '''+esc(model["document_id"])+'''，版本 '''+esc(model["revision"])+'''。默认只读计划，逐项条件写入；真实 ION / MES 未接入。</p><section class="cards">'''+card_html+'''</section><section class="panel"><div class="bar">12 个步骤的实际计划与核实结果<span>源 PDF 3 页 · 保留原有额外步骤 1 条</span></div><div class="scroll"><table><thead><tr><th>步骤</th><th>提取标题</th><th>来源页</th><th>初始计划</th><th>HTTP 执行</th><th>再次计划</th></tr></thead><tbody>'''+"".join(rows)+'''</tbody></table></div></section><div class="foot"><div>实际请求及执行记录已保存。模拟服务已停止，此页为已保存结果。<br>未知标记、步骤冲突或过期计划会拒绝执行；部分失败保留逐项记录。</div><div>远端最终 13 条，额外记录原样保留。<br>演示接口为自定义约定，不是 ION 官方接口。</div></div><div class="hash">源 SHA-256：'''+esc(report["source_sha256"])+'''</div></main></html>'''


def run_demo(output_dir,source=None):
    engine=module("engine3077_demo","3077_提取同步引擎.py")
    mock=module("mock3077_demo","3077_本地模拟接口.py")
    fixture=module("fixture3077_demo","3077_自造文档.py")
    source=engine.safe_path(source or HERE/"3077_自造作业说明.pdf")
    output_dir=engine.safe_path(output_dir)
    if output_dir.exists() or not output_dir.parent.is_dir():
        raise ValueError("演示结果目录必须不存在且父目录存在")
    original=hashlib.sha256(source.read_bytes()).hexdigest()
    model=engine.extract_pdf(source)
    expected=fixture.expected_model()
    assert model["document_id"]==expected["document_id"] and model["revision"]==expected["revision"]
    assert model["steps"]==expected["steps"] and len(model["steps"])==12 and model["page_count"]==3
    payloads=[{"document_id":model["document_id"],"revision":model["revision"],**step} for step in model["steps"]]
    initial={}
    for i,payload in enumerate(payloads[:8]):
        item=copy.deepcopy(payload)
        if i>=4:
            item["instructions"]="Older local training text."
        initial[engine.validate_payload(item)]=item
    extra={"document_id":model["document_id"],"revision":model["revision"],"step_id":"999","title":"Retained local training record","instructions":"Keep this synthetic remote-only record unchanged.","source_pages":[1]}
    initial[engine.validate_payload(extra)]=extra
    temp=Path(tempfile.mkdtemp(prefix="3077_演示暂存_",dir=output_dir.parent))
    server=None
    try:
        server=mock.start_mock(initial)
        remote=engine.get_remote(server.base_url)
        plan=engine.build_plan(model,remote,server.base_url)
        counts=Counter(op["action"] for op in plan["operations"])
        assert counts=={"create":4,"update":4,"unchanged":4}
        count_put=lambda:sum(r["method"]=="PUT" for r in server.request_log)
        assert count_put()==0
        engine.write_json(temp/"3077_提取步骤.json",model,new=True)
        engine.write_json(temp/"3077_同步计划.json",plan,new=True)
        journal=engine.apply_plan(source,plan,server.base_url,temp/"3077_执行记录.json")
        assert journal["complete"] and journal["verified_writes"]==8 and journal["attempted_writes"]==8
        before_rerun=count_put()
        replay=engine.build_plan(model,engine.get_remote(server.base_url),server.base_url)
        assert all(op["action"]=="unchanged" for op in replay["operations"])
        engine.write_json(temp/"3077_重跑计划.json",replay,new=True)
        rerun=engine.apply_plan(source,replay,server.base_url,temp/"3077_重跑记录.json")
        assert rerun["complete"] and rerun["attempted_writes"]==rerun["verified_writes"]==0 and rerun["unchanged"]==12
        assert count_put()==before_rerun
        after=server.snapshot()
        assert len(after)==13 and after[engine.validate_payload(extra)]==extra
        assert all(after[engine.validate_payload(p)]==p for p in payloads)
        assert hashlib.sha256(source.read_bytes()).hexdigest()==original
        report={"source_sha256":original,"source_pages":3,"extracted_steps":12,"initial_remote_steps":9,"planned_create":4,"planned_update":4,"planned_unchanged":4,
                "verified_writes":journal["verified_writes"],"write_requests":before_rerun,"rerun_unchanged":12,"rerun_write_requests":count_put()-before_rerun,"final_remote_steps":13,"extra_preserved":1,
                "all_extracted_fields_match_independent_oracle":True,"all_remote_payloads_exact":True,"source_unchanged":True,"only_loopback_mock_used":True,"real_ion_connected":False,"ocr_used":False,"desktop_gui":False}
        log=copy.deepcopy(server.request_log)
        server.stop();server=None
        report["mock_server_stopped"]=True
        engine.write_json(temp/"3077_模拟状态.json",after,new=True)
        engine.write_json(temp/"3077_请求记录.json",log,new=True)
        engine.write_json(temp/"3077_演示核验.json",report,new=True)
        with (temp/"3077_验收展示.html").open("x",encoding="utf-8",newline="\n") as f:
            f.write(preview(model,plan,journal,replay,report))
        if output_dir.exists():
            raise ValueError("目标目录在运行中出现，拒绝覆盖")
        temp.rename(output_dir)
        return report
    finally:
        if server is not None:
            server.stop()
        if temp.exists():
            if temp.parent.resolve()!=output_dir.parent.resolve() or not temp.name.startswith("3077_演示暂存_"):
                raise RuntimeError("临时目录超出本次拥有范围")
            shutil.rmtree(temp)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--source",type=Path)
    args=parser.parse_args()
    try:
        print(json.dumps(run_demo(args.output,args.source),ensure_ascii=False,indent=2))
    except Exception as exc:
        parser.exit(1,f"演示失败：{exc}\n")
