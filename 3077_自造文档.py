# SPDX-License-Identifier: MIT
"""3077：生成独立虚构训练PDF和固定逐字段oracle；没有真实客户或MES数据。"""
from __future__ import annotations

import argparse
import copy
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import textwrap

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

DOCUMENT_ID = "DEMO-WI-001"
REVISION = "A"
CONTENT = [
    ("Open the fictional training panel", "Open the fictional training panel described in this exercise. Keep the session in demonstration mode and do not connect to any operational system."),
    ("Select the practice record", "Select the sample record named TRAINING-ALPHA from the fictional record list. Confirm that its visible label says synthetic training data."),
    ("Check the demonstration label", "Read the demonstration label on the practice record. Compare the displayed document code with DEMO-WI-001 and leave all real records unchanged."),
    ("Enter the sample session code", "Enter the sample session code PRACTICE-001 in the training-only text field. Review the characters before continuing to the next fictional step."),
    ("Review the simulated step list", "Review the simulated step list in the training panel. Confirm that the list begins with step 001 and contains twelve fictional steps."),
    ("Mark the practice item reviewed", "Set the practice-only review flag for the selected sample item. Add no real approval or production status to any external application."),
    ("Add a training-only comment", "Add the comment Training example reviewed to the sample comment field. Keep personal details and real customer information out of the exercise."),
    ("Save the temporary practice result", "Save the fictional practice result in the demonstration session. Confirm that the training panel displays the sample saved indicator."),
    ("Reopen the saved practice result", "Reopen the saved sample result from the fictional session history. Verify that the practice session code and training-only comment are visible."),
    ("Compare the displayed sample code", "Compare the displayed sample code with PRACTICE-001. If the characters differ in this exercise, record the difference in a training-only note."),
    ("Record the training completion note", "Record the note Synthetic exercise complete in the sample result. This note documents only the fictional training session and is not a real work approval."),
    ("Close the fictional training session", "Review the saved practice result one final time and close the fictional session. The exercise ends without sending information to a real operational system."),
]


def expected_model():
    return dict(document_id=DOCUMENT_ID, revision=REVISION,
                steps=[dict(step_id=f"{index + 1:03d}", title=title, instructions=instructions,
                            source_pages=[index // 4 + 1]) for index, (title, instructions) in enumerate(CONTENT)])


def desired_payloads():
    model = expected_model()
    return {f"{DOCUMENT_ID}:{step['step_id']}": dict(document_id=DOCUMENT_ID, revision=REVISION, **step) for step in model["steps"]}


def initial_remote():
    """初始9条：4缺失待创建、4旧内容待更新、4相同不写，另保留1条。"""
    desired = desired_payloads()
    result = {}
    for index, (key, payload) in enumerate(desired.items()):
        if index < 4:
            continue
        result[key] = copy.deepcopy(payload)
        if index < 8:
            result[key]["instructions"] = f"Previous synthetic instruction for training step {payload['step_id']}."
    result[f"{DOCUMENT_ID}:900"] = dict(document_id=DOCUMENT_ID, revision=REVISION, step_id="900", title="Retained extra training item",
                                        instructions="Keep this extra synthetic training record unchanged.", source_pages=[1])
    return result


def page_lines():
    pages = []
    model = expected_model()
    for page in range(3):
        lines = ["DEMO WORK INSTRUCTION", "SYNTHETIC TRAINING DATA", f"Document: {DOCUMENT_ID}", f"Revision: {REVISION}"]
        for step in model["steps"][page * 4:(page + 1) * 4]:
            lines.append(f"STEP {step['step_id']}: {step['title']}")
            lines.extend(textwrap.wrap("Instruction: " + step["instructions"], width=87, break_long_words=False, break_on_hyphens=False))
            lines.append("END STEP")
        pages.append(lines)
    return pages


def pdf_bytes(pages, *, footer_document=DOCUMENT_ID, footer_revision=REVISION):
    """共享绘制器也供owned-temp边界验收；仅正式generate会发布PDF文件。"""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, pageCompression=1, invariant=1)
    pdf.setTitle("3077 Synthetic Training Work Instruction")
    pdf.setAuthor("Independent synthetic portfolio demonstration")
    pdf.setSubject("Fictional training data. No real customer document or operational integration.")
    navy, muted = HexColor("#244A6A"), HexColor("#637286")
    for number, lines in enumerate(pages, 1):
        y = letter[1] - 54
        for line in lines:
            if not isinstance(line, str) or not line.isascii():
                raise ValueError("自造训练文档仅接受ASCII文本")
            if line == "DEMO WORK INSTRUCTION":
                font, size, color, gap = "Helvetica-Bold", 14, navy, 22
            elif line == "SYNTHETIC TRAINING DATA":
                font, size, color, gap = "Helvetica", 9, muted, 24
            elif line.startswith("STEP "):
                font, size, color, gap = "Helvetica-Bold", 10, navy, 18
            elif line == "END STEP":
                font, size, color, gap = "Helvetica", 9, muted, 38
            else:
                font, size, color, gap = "Helvetica", 10, navy, 15
            if y < 88 or stringWidth(line, font, size) > letter[0] - 108:
                raise ValueError("自造文档文字超出安全版面，不截断或缩小")
            pdf.setFont(font, size)
            pdf.setFillColor(color)
            pdf.drawString(54, y, line)
            if line.startswith("Revision:"):
                pdf.setStrokeColor(HexColor("#CFD9E3"))
                pdf.setLineWidth(0.5)
                pdf.line(54, y - 10, letter[0] - 54, y - 10)
                gap = 34
            y -= gap
        pdf.setStrokeColor(HexColor("#CFD9E3"))
        pdf.setLineWidth(0.5)
        pdf.line(54, 69, letter[0] - 54, 69)
        pdf.setFillColor(muted)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(54, 54, f"{footer_document} - {footer_revision}")
        pdf.drawRightString(letter[0] - 54, 54, f"Page {number} of {len(pages)}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def generate(output):
    output = Path(os.path.abspath(output))
    for path in (output, *output.parents):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("不接受符号链接或目录连接点输出")
    if output.exists() or not output.parent.is_dir() or output.suffix.lower() != ".pdf":
        raise ValueError("输出须为已有父目录下的全新PDF文件，禁止覆盖")
    data = pdf_bytes(page_lines())
    with output.open("xb") as stream:
        stream.write(data)
    return dict(synthetic=True, document_id=DOCUMENT_ID, revision=REVISION, page_count=3, steps=12,
                source_sha256=hashlib.sha256(data).hexdigest(), source_name=output.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    print(json.dumps(generate(parser.parse_args().output), ensure_ascii=True, indent=2))
