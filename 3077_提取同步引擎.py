# SPDX-License-Identifier: MIT
"""明确标记文字PDF -> 只读计划 -> 自有回环模拟接口条件同步。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

DOCUMENT = re.compile(r"DEMO-WI-[A-Z0-9]{1,20}")
REVISION = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,15}")
STEP = re.compile(r"STEP ([0-9]{3}): (.+)")
PAYLOAD_FIELDS = {"document_id","revision","step_id","title","instructions","source_pages"}
MAX_PDF_BYTES=10_000_000
MAX_TEXT=200_000


class SyncError(ValueError):
    """已保留执行记录；远端可能已有部分成功项。"""


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def safe_path(path):
    path=Path(os.path.abspath(path))
    for item in (path,*path.parents):
        if item.exists() and (item.is_symlink() or getattr(item,"is_junction",lambda:False)()):
            raise ValueError("不支持符号链接或目录连接点路径")
    return path


def source_bytes(source):
    source=safe_path(source)
    if source.suffix.lower()!=".pdf" or not source.is_file() or source.stat().st_size>MAX_PDF_BYTES:
        raise ValueError("仅支持不超过10MB的普通PDF")
    raw=source.read_bytes()
    if len(raw)>MAX_PDF_BYTES:
        raise ValueError("源PDF读取期间变大")
    return source,raw


def extract_pdf(source):
    from io import BytesIO
    from pypdf import PdfReader
    source,raw=source_bytes(source)
    try:
        reader=PdfReader(BytesIO(raw),strict=True)
        if reader.is_encrypted:
            raise ValueError("不支持加密PDF")
        root=reader.trailer["/Root"]
        if root.get("/AcroForm") or root.get("/Collection") or root.get("/OpenAction"):
            raise ValueError("不支持交互表单、文档组合或打开动作")
        if not 1<=len(reader.pages)<=20:
            raise ValueError("PDF应为1至20页")
        texts=[p.extract_text(extraction_mode="plain") or "" for p in reader.pages]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"PDF不能可靠解析：{type(exc).__name__}") from exc
    if sum(map(len,texts))>MAX_TEXT:
        raise ValueError("PDF提取文字超过200000字符")
    document_id=revision=None
    steps=[]
    active=None
    previous=0
    for page_number,text in enumerate(texts,1):
        lines=[" ".join(line.split()) for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"第{page_number}页无文字；不进行OCR")
        if any(lines.count(banner)!=1 for banner in ("DEMO WORK INSTRUCTION","SYNTHETIC TRAINING DATA")):
            raise ValueError(f"第{page_number}页缺少唯一的独立演示标题或训练数据标记")
        ids=[line[len("Document: "):] for line in lines if line.startswith("Document: ")]
        revs=[line[len("Revision: "):] for line in lines if line.startswith("Revision: ")]
        if len(ids)!=1 or len(revs)!=1 or not DOCUMENT.fullmatch(ids[0]) or not REVISION.fullmatch(revs[0]):
            raise ValueError(f"第{page_number}页文档/版本标记不完整或有歧义")
        if document_id is None:
            document_id,revision=ids[0],revs[0]
        if (ids[0],revs[0])!=(document_id,revision):
            raise ValueError("各页文档编号或版本不一致")
        if active and page_number not in active["source_pages"]:
            active["source_pages"].append(page_number)
        allowed={"DEMO WORK INSTRUCTION","SYNTHETIC TRAINING DATA",f"Document: {document_id}",f"Revision: {revision}",f"{document_id} - {revision}",f"Page {page_number} of {len(texts)}"}
        for line in lines:
            if line in allowed:
                continue
            found=STEP.fullmatch(line)
            if found:
                if active is not None:
                    raise ValueError("出现新步骤时前一步尚无END STEP")
                number=int(found[1])
                if not previous<number<=999:
                    raise ValueError("步骤编号重复、为零或未按严格升序排列")
                title=found[2].strip()
                if not title or len(title)>100:
                    raise ValueError("步骤标题为空或超过100字符")
                active={"step_id":found[1],"title":title,"source_pages":[page_number],"parts":[],"instruction_started":False}
                previous=number
            elif line=="END STEP":
                if active is None or not active["instruction_started"] or not active["parts"]:
                    raise ValueError("END STEP缺少完整步骤正文")
                instructions=" ".join(active.pop("parts"))
                active.pop("instruction_started")
                if len(instructions)>5000:
                    raise ValueError("单步正文超过5000字符")
                active["instructions"]=instructions
                steps.append(active)
                active=None
            elif line.startswith("Instruction: "):
                if active is None or active["instruction_started"]:
                    raise ValueError("Instruction标记缺步骤或重复")
                part=line[len("Instruction: "):].strip()
                if not part:
                    raise ValueError("步骤正文为空")
                active["instruction_started"]=True
                active["parts"].append(part)
            elif active is not None and active["instruction_started"]:
                if line.startswith(("STEP ","Document:","Revision:","Page ","Instruction:")):
                    raise ValueError("正文中存在不合法或歧义保留标记")
                active["parts"].append(line)
            else:
                raise ValueError(f"第{page_number}页存在未识别标记或步骤外文字")
    if active is not None or not steps:
        raise ValueError("步骤未结束或未发现步骤")
    if len(steps)>999:
        raise ValueError("步骤数超过999")
    if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256(raw).digest():
        raise ValueError("源文件在读取期间改变")
    return {"source_sha256":hashlib.sha256(raw).hexdigest(),"source_name":source.name,"page_count":len(texts),"document_id":document_id,"revision":revision,"steps":steps}


def validate_payload(payload):
    if not isinstance(payload,dict) or set(payload)!=PAYLOAD_FIELDS:
        raise ValueError("步骤负载字段不匹配本地模拟约定")
    if not isinstance(payload["document_id"],str) or not DOCUMENT.fullmatch(payload["document_id"]):
        raise ValueError("文档编号无效")
    if not isinstance(payload["revision"],str) or not REVISION.fullmatch(payload["revision"]):
        raise ValueError("版本无效")
    sid=payload["step_id"]
    if not isinstance(sid,str) or not re.fullmatch(r"[0-9]{3}",sid) or not 1<=int(sid)<=999:
        raise ValueError("步骤编号无效")
    for field,limit in (("title",100),("instructions",5000)):
        if not isinstance(payload[field],str) or not payload[field].strip() or len(payload[field])>limit:
            raise ValueError(f"{field}为空、类型无效或过长")
    pages=payload["source_pages"]
    if not isinstance(pages,list) or not pages or any(type(p) is not int or not 1<=p<=20 for p in pages) or pages!=sorted(set(pages)):
        raise ValueError("来源页号无效")
    return f"{payload['document_id']}:{sid}"


def endpoint(base_url):
    try:
        parsed=urlsplit(base_url)
        port=parsed.port
    except (ValueError,TypeError) as exc:
        raise ValueError("模拟接口地址无效") from exc
    if parsed.scheme!="http" or parsed.hostname!="127.0.0.1" or not port or not 1<=port<=65535 or parsed.username is not None or parsed.password is not None or parsed.path not in ("","/") or parsed.query or parsed.fragment:
        raise ValueError("仅允许http://127.0.0.1:端口的自有模拟接口")
    return f"http://127.0.0.1:{port}"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise ValueError("模拟接口不允许HTTP重定向")


def request_json(base_url,method,path,payload=None,headers=None):
    base_url=endpoint(base_url)
    if not path.startswith("/demo/steps"):
        raise ValueError("模拟接口路径无效")
    actual_headers={"Accept":"application/json","X-Demo-Marker":"3077"}
    if headers:
        actual_headers.update(headers)
    data=canonical(payload) if payload is not None else None
    if data is not None:
        actual_headers["Content-Type"]="application/json; charset=utf-8"
    request=Request(base_url+path,data=data,headers=actual_headers,method=method)
    with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=3) as response:
        if response.headers.get("X-Demo-Marker")!="3077" or response.headers.get_content_type()!="application/json":
            raise ValueError("目标不是3077本地模拟JSON接口")
        raw=response.read(1_000_001)
        if len(raw)>1_000_000:
            raise ValueError("模拟接口响应超过1MB")
        return json.loads(raw.decode("utf-8"))


def get_remote(base_url):
    snapshot=request_json(base_url,"GET","/demo/steps")
    if not isinstance(snapshot,dict) or set(snapshot)!={"demo_marker","steps"} or snapshot["demo_marker"]!="3077" or not isinstance(snapshot["steps"],list) or len(snapshot["steps"])>1000:
        raise ValueError("远端快照结构不匹配模拟约定")
    keys=set()
    for item in snapshot["steps"]:
        if not isinstance(item,dict) or set(item)!={"key","etag","payload"}:
            raise ValueError("远端步骤结构不完整")
        key=validate_payload(item["payload"])
        if item["key"]!=key or key in keys or item["etag"]!='"'+digest(item["payload"])+'"':
            raise ValueError("远端步骤编号、唯一性或ETag不匹配")
        keys.add(key)
    return {"demo_marker":"3077","steps":sorted(snapshot["steps"],key=lambda s:s["key"])}


def build_plan(model,snapshot,base_url):
    base_url=endpoint(base_url)
    remote={r["key"]:r for r in snapshot["steps"]}
    if len(remote)!=len(snapshot["steps"]):
        raise ValueError("远端快照存在重复键")
    operations=[]
    desired=[]
    for step in model["steps"]:
        payload={"document_id":model["document_id"],"revision":model["revision"],**step}
        key=validate_payload(payload)
        if key in [x["key"] for x in operations]:
            raise ValueError("源步骤重复")
        current=remote.get(key)
        action="create" if current is None else "unchanged" if current["payload"]==payload else "update"
        operations.append({"key":key,"action":action,"expected_etag":current["etag"] if current else None,"payload":payload})
        desired.append(payload)
    if not operations:
        raise ValueError("没有可计划的步骤")
    return {"schema_version":1,"endpoint":base_url,"source_sha256":model["source_sha256"],"document_id":model["document_id"],"revision":model["revision"],
            "remote_snapshot_sha256":digest(snapshot),"desired_sha256":digest(desired),"operations":operations,
            "unmodified_extra_keys":sorted(set(remote)-{x["key"] for x in operations})}


def write_json(path,value,new=False):
    path=safe_path(path)
    if not path.parent.is_dir():
        raise ValueError("输出文件父目录不存在")
    text=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n"
    if new:
        with path.open("x",encoding="utf-8",newline="\n") as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
    else:
        fd,temporary=tempfile.mkstemp(prefix="3077_记录暂存_",dir=path.parent)
        temporary=Path(temporary)
        try:
            with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as stream:
                stream.write(text);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)


def apply_plan(source,plan,base_url,journal_path):
    base_url=endpoint(base_url)
    source=safe_path(source)
    journal_path=safe_path(journal_path)
    if journal_path.exists() or not journal_path.parent.is_dir():
        raise ValueError("执行记录必须是父目录存在的新文件")
    current_model=extract_pdf(source)
    current_remote=get_remote(base_url)
    expected=build_plan(current_model,current_remote,base_url)
    if plan!=expected:
        raise ValueError("源PDF、计划内容或远端快照已改变；请重新生成计划")
    journal={"schema_version":1,"source_sha256":plan["source_sha256"],"plan_sha256":digest(plan),"endpoint":base_url,
             "complete":False,"attempted_writes":0,"verified_writes":0,"unchanged":0,"writes":[],"error":None,"extra_keys_preserved":plan["unmodified_extra_keys"]}
    write_json(journal_path,journal,new=True)
    try:
        for operation in plan["operations"]:
            if hashlib.sha256(source.read_bytes()).hexdigest()!=plan["source_sha256"]:
                raise ValueError("执行期间源PDF改变")
            if operation["action"]=="unchanged":
                journal["unchanged"]+=1
                continue
            path="/demo/steps/"+quote(operation["key"],safe="")
            condition={"If-None-Match":"*"} if operation["action"]=="create" else {"If-Match":operation["expected_etag"]}
            journal["attempted_writes"]+=1
            event={"key":operation["key"],"action":operation["action"],"verified":False,"response_recovered":False}
            journal["writes"].append(event)
            write_json(journal_path,journal)
            request_error=None
            try:
                request_json(base_url,"PUT",path,operation["payload"],condition)
            except (HTTPError,URLError,TimeoutError,OSError,ValueError) as exc:
                request_error=exc
            try:
                confirmed=request_json(base_url,"GET",path)
                correct=(isinstance(confirmed,dict) and confirmed.get("key")==operation["key"] and confirmed.get("payload")==operation["payload"] and confirmed.get("etag")=='"'+digest(operation["payload"])+'"')
            except (HTTPError,URLError,TimeoutError,OSError,ValueError):
                correct=False
            if not correct:
                event["uncertain_or_not_applied"]=True
                if request_error:
                    raise ValueError(f"写入未能核实：{type(request_error).__name__}") from request_error
                raise ValueError("写入响应后读取的内容不匹配")
            event.update(verified=True,response_recovered=request_error is not None)
            journal["verified_writes"]+=1
            write_json(journal_path,journal)
        after=get_remote(base_url)
        final={r["key"]:r["payload"] for r in after["steps"]}
        if any(final.get(op["key"])!=op["payload"] for op in plan["operations"]):
            raise ValueError("最终状态与计划不一致，可能存在并发修改")
        if any(key not in final for key in plan["unmodified_extra_keys"]):
            raise ValueError("额外远端记录在执行期间消失")
        if hashlib.sha256(source.read_bytes()).hexdigest()!=plan["source_sha256"]:
            raise ValueError("执行结束时源PDF已改变")
        journal.update(complete=True,remote_snapshot_after_sha256=digest(after))
        write_json(journal_path,journal)
        return journal
    except Exception as exc:
        journal["error"]={"type":type(exc).__name__,"message":str(exc),"note":"已核实写入仍保留；没有整批远端回滚，请读取记录并重新生成计划。"}
        write_json(journal_path,journal)
        raise SyncError(str(exc)) from exc


def main():
    parser=argparse.ArgumentParser(description="3077 明确标记文字PDF的步骤提取与本地模拟同步，非ION官方API")
    subs=parser.add_subparsers(dest="command",required=True)
    extract=subs.add_parser("extract",help="只读提取，不联网")
    extract.add_argument("--source",required=True,type=Path);extract.add_argument("--output",required=True,type=Path)
    plan=subs.add_parser("plan",help="只读获取本地快照并生成计划，不写远端")
    plan.add_argument("--source",required=True,type=Path);plan.add_argument("--endpoint",required=True);plan.add_argument("--output",required=True,type=Path)
    apply=subs.add_parser("apply",help="明确执行既有计划，仅自有127.0.0.1模拟接口")
    apply.add_argument("--source",required=True,type=Path);apply.add_argument("--endpoint",required=True);apply.add_argument("--plan",required=True,type=Path);apply.add_argument("--journal",required=True,type=Path)
    args=parser.parse_args()
    try:
        if args.command=="apply":
            plan_path=safe_path(args.plan)
            if plan_path.stat().st_size>2_000_000:
                raise ValueError("计划文件超过2MB")
            result=apply_plan(args.source,json.loads(plan_path.read_text(encoding="utf-8")),args.endpoint,args.journal)
        else:
            model=extract_pdf(args.source)
            result=model if args.command=="extract" else build_plan(model,get_remote(args.endpoint),args.endpoint)
            write_json(args.output,result,new=True)
        print(json.dumps({"command":args.command,"successful":True},ensure_ascii=False))
    except Exception as exc:
        parser.exit(1,f"处理失败：{exc}\n")


if __name__=="__main__":
    main()
