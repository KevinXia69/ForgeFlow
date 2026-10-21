"""Build the 18-slide Chinese ForgeFlow first-version deck with python-pptx.

The content is limited to facts recorded in the repository:

* ``ForgeFlow_工程进度.md`` (P1 probe, permission-split run, attestation);
* ``docs/首版演示与证据.md`` and ``docs/P1_权限拆分与政策合同.md``;
* ``README.md``, ``contracts/README.md`` and the v3 plan section 12.3.

No model is called, no container is started and no secret is read. The output
is written atomically and normalised so repeated runs produce identical bytes.
"""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Inches, Pt

SLIDE_WIDTH = Emu(12192000)  # 13.333 in, exact 16:9
SLIDE_HEIGHT = Emu(6858000)
SLIDE_COUNT = 18

INK = RGBColor(0x16, 0x1A, 0x20)
MUTED = RGBColor(0x5C, 0x64, 0x70)
FAINT = RGBColor(0x8A, 0x91, 0x9B)
ACCENT = RGBColor(0xE0, 0x5A, 0x28)
ACCENT_SOFT = RGBColor(0xFB, 0xE9, 0xE0)
DARK = RGBColor(0x12, 0x16, 0x1C)
DARK_SOFT = RGBColor(0x1D, 0x23, 0x2B)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
PAPER = RGBColor(0xFF, 0xFF, 0xFF)
SOFT = RGBColor(0xF5, 0xF2, 0xED)
SOFT_2 = RGBColor(0xEF, 0xEB, 0xE4)
LINE = RGBColor(0xDD, 0xD7, 0xCE)
DARK_LINE = RGBColor(0x34, 0x3B, 0x45)

# PowerPoint resolves a missing East Asian face through the OS; macOS uses
# PingFang SC when Microsoft YaHei is unavailable.
CJK_FONT = "Microsoft YaHei"
CJK_FALLBACK = "PingFang SC"

DEFAULT_OUT = Path("docs") / "ForgeFlow_首版_18页.pptx"


def _style_run(
    run,
    *,
    size: float,
    bold: bool = False,
    color: RGBColor = INK,
    italic: bool = False,
) -> None:
    font = run.font
    font.name = CJK_FONT
    font.size = Pt(size)
    font.bold = bold
    font.italic = italic
    font.color.rgb = color
    rpr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        element = rpr.find(qn(tag))
        if element is None:
            element = rpr.makeelement(qn(tag), {})
            rpr.append(element)
        element.set("typeface", CJK_FONT)


def _set_background(slide, color: RGBColor) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _suppress_theme_effect(item) -> None:
    style = item._element.find(qn("p:style"))
    if style is None:
        return
    effect = style.find(qn("a:effectRef"))
    if effect is not None:
        effect.set("idx", "0")


def _add_rect(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: RGBColor | None = SOFT,
    line: RGBColor | None = LINE,
    width: float = 0.75,
    radius: float | None = 0.08,
    shape=MSO_SHAPE.ROUNDED_RECTANGLE,
):
    item = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill is None:
        item.fill.background()
    else:
        item.fill.solid()
        item.fill.fore_color.rgb = fill
    if line is None:
        item.line.fill.background()
    else:
        item.line.color.rgb = line
        item.line.width = Pt(width)
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        item.adjustments[0] = radius
    item.shadow.inherit = False
    _suppress_theme_effect(item)
    return item


def _add_text(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str | list[str],
    *,
    size: float = 12,
    bold: bool = False,
    color: RGBColor = INK,
    align=PP_ALIGN.LEFT,
    anchor=MSO_ANCHOR.TOP,
    spacing: float = 1.12,
    gap: float = 0,
    italic: bool = False,
):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = Inches(0.02)
    frame.margin_right = Inches(0.02)
    frame.margin_top = Inches(0)
    frame.margin_bottom = Inches(0)
    frame.vertical_anchor = anchor
    lines = [text] if isinstance(text, str) else list(text)
    for index, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = align
        paragraph.line_spacing = spacing
        paragraph.space_after = Pt(gap)
        run = paragraph.add_run()
        run.text = line
        _style_run(run, size=size, bold=bold, color=color, italic=italic)
    return box


def _add_bullets(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    items: list[str],
    *,
    size: float = 11,
    color: RGBColor = INK,
    bullet_color: RGBColor = ACCENT,
    spacing: float = 1.1,
    gap: float = 4,
):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = Inches(0.02)
    frame.margin_right = Inches(0.02)
    frame.margin_top = Inches(0)
    frame.margin_bottom = Inches(0)
    for index, item in enumerate(items):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.line_spacing = spacing
        paragraph.space_after = Pt(gap)
        mark = paragraph.add_run()
        mark.text = "·  "
        _style_run(mark, size=size, bold=True, color=bullet_color)
        body = paragraph.add_run()
        body.text = item
        _style_run(body, size=size, color=color)
    return box


def _add_pill(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    *,
    fill: RGBColor = ACCENT,
    color: RGBColor = WHITE,
    size: float = 8.5,
):
    _add_rect(slide, x, y, w, h, fill=fill, line=None, radius=0.5)
    _add_text(
        slide,
        x,
        y,
        w,
        h,
        text,
        size=size,
        bold=True,
        color=color,
        align=PP_ALIGN.CENTER,
        anchor=MSO_ANCHOR.MIDDLE,
    )


def _add_number_badge(
    slide,
    x: float,
    y: float,
    diameter: float,
    text: str,
    *,
    fill: RGBColor = ACCENT,
    color: RGBColor = WHITE,
    size: float = 12,
):
    item = _add_rect(
        slide, x, y, diameter, diameter, fill=fill, line=None, shape=MSO_SHAPE.OVAL, radius=None
    )
    _add_text(
        slide,
        x,
        y,
        diameter,
        diameter,
        text,
        size=size,
        bold=True,
        color=color,
        align=PP_ALIGN.CENTER,
        anchor=MSO_ANCHOR.MIDDLE,
    )
    return item


def _add_arrow(
    slide,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    color: RGBColor = ACCENT,
    width: float = 1.5,
    dashed: bool = False,
):
    connector = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2)
    )
    connector.line.color.rgb = color
    connector.line.width = Pt(width)
    _suppress_theme_effect(connector)
    if dashed:
        connector.line.dash_style = 4
    line_xml = connector.line._get_or_add_ln()
    tail = OxmlElement("a:tailEnd")
    tail.set("type", "triangle")
    tail.set("w", "med")
    tail.set("len", "med")
    line_xml.append(tail)
    return connector


def _add_rule(slide, x: float, y: float, w: float, *, color: RGBColor = LINE) -> None:
    _add_rect(slide, x, y, w, 0.012, fill=color, line=None, shape=MSO_SHAPE.RECTANGLE, radius=None)


def _page(slide, page: int, *, dark: bool = False) -> None:
    _add_text(
        slide,
        12.05,
        7.08,
        0.72,
        0.25,
        f"{page:02d} / {SLIDE_COUNT}",
        size=7.5,
        color=FAINT if not dark else RGBColor(0x8B, 0x93, 0x9E),
        align=PP_ALIGN.RIGHT,
    )


def _footer(slide, text: str, page: int) -> None:
    _add_rule(slide, 0.55, 6.98, 12.22, color=LINE)
    _add_rect(slide, 0.55, 7.10, 0.055, 0.055, fill=ACCENT, line=None, radius=None,
              shape=MSO_SHAPE.RECTANGLE)
    _add_text(slide, 0.69, 7.03, 11.2, 0.3, text, size=7.5, color=MUTED)
    _page(slide, page)


def _content_slide(prs, title: str, kicker: str, evidence: str, page: int):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, PAPER)
    _add_rect(slide, 0.55, 0.36, 0.075, 0.52, fill=ACCENT, line=None, radius=None,
              shape=MSO_SHAPE.RECTANGLE)
    _add_text(slide, 0.76, 0.27, 10.7, 0.62, title, size=23, bold=True, color=INK)
    _add_text(slide, 0.77, 0.91, 11.7, 0.3, kicker, size=10.5, color=MUTED)
    _add_text(
        slide,
        10.9,
        0.38,
        1.9,
        0.24,
        "FORGEFLOW · 首版",
        size=7.5,
        bold=True,
        color=FAINT,
        align=PP_ALIGN.RIGHT,
    )
    _add_rule(slide, 0.55, 1.27, 12.22)
    _footer(slide, evidence, page)
    return slide


def _card_title(slide, x, y, w, title, *, size=14, color=INK):
    _add_text(slide, x, y, w, 0.32, title, size=size, bold=True, color=color)


def _kicker_label(slide, x, y, text, *, color=ACCENT, w=2.4):
    _add_text(slide, x, y, w, 0.24, text, size=8.5, bold=True, color=color)


def _metric_tile(slide, x, y, w, h, value, label, *, note=None):
    _add_rect(slide, x, y, w, h, fill=WHITE, line=LINE, radius=0.06)
    _add_text(slide, x + 0.16, y + 0.12, w - 0.3, 0.46, value, size=24, bold=True, color=ACCENT)
    _add_text(slide, x + 0.16, y + 0.58, w - 0.3, 0.3, label, size=9.5, color=MUTED)
    if note:
        _add_text(slide, x + 0.16, y + h - 0.3, w - 0.3, 0.22, note, size=7.5, color=FAINT)


# ---------------------------------------------------------------------------
# Slides
# ---------------------------------------------------------------------------


def _slide_cover(prs) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, DARK)
    _add_rect(slide, 0.7, 1.18, 0.09, 2.55, fill=ACCENT, line=None, radius=None,
              shape=MSO_SHAPE.RECTANGLE)
    _add_text(slide, 1.02, 1.02, 8.4, 0.3, "NEBIUS × NVIDIA GLOBAL AI HACKATHON",
              size=10, bold=True, color=ACCENT)
    _add_text(slide, 1.02, 1.42, 8.7, 0.95, "ForgeFlow", size=54, bold=True, color=WHITE)
    _add_text(slide, 1.04, 2.4, 8.6, 0.62, "制造业生产与交付异常处置智能体",
              size=25, color=WHITE)
    _add_text(slide, 1.04, 3.15, 8.6, 0.4, "从“目标态”到可审计的增量执行",
              size=15, color=ACCENT)
    _add_rule(slide, 1.04, 4.05, 7.5, color=DARK_LINE)
    _add_text(
        slide,
        1.04,
        4.25,
        8.7,
        0.62,
        "首版材料：两个开发场景、一条受控执行链、原始验收与失败记录并存。",
        size=11,
        color=RGBColor(0xC8, 0xCE, 0xD6),
    )

    _add_rect(slide, 9.95, 1.55, 1.05, 1.05, fill=DARK_SOFT, line=DARK_LINE, radius=0.08)
    _add_text(slide, 9.95, 1.55, 1.05, 1.05, "A", size=26, bold=True, color=WHITE,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    _add_arrow(slide, 11.12, 2.08, 11.62, 2.08, color=ACCENT, width=1.6)
    _add_rect(slide, 11.8, 1.55, 1.05, 1.05, fill=ACCENT, line=None, radius=0.08)
    _add_text(slide, 11.8, 1.55, 1.05, 1.05, "B", size=26, bold=True, color=WHITE,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    _add_text(slide, 9.95, 2.85, 2.9, 0.5, "供应商断供 / 产线停机", size=9.5, color=FAINT)

    _add_rule(slide, 1.04, 5.72, 11.7, color=DARK_LINE)
    for x, label, value in (
        (1.04, "任务范围", "A 断供 · B 停机"),
        (4.7, "验收方式", "ERP-Bench 原始规则"),
        (8.36, "材料边界", "A 3/3、B 2/3；保留任务 0/1"),
    ):
        _add_text(slide, x, 5.94, 3.3, 0.24, label, size=8, bold=True, color=ACCENT)
        _add_text(slide, x, 6.2, 3.35, 0.3, value, size=10.5, color=WHITE)
    _add_text(slide, 1.04, 6.78, 10.5, 0.25, "所有结果绑定 run_id；未测量项保持未知。",
              size=8, color=FAINT)
    _page(slide, 1, dark=True)


def _slide_problem(prs) -> None:
    slide = _content_slide(
        prs,
        "问题：两类制造异常，打断同一条交付链",
        "不是聊天题：要在真实 Odoo 里重排采购与制造计划，并接受原始规则验收",
        "来源：ERP-Bench 开发任务 2280 / 2299（合成业务数据）· 配置：planning-metadata-v1（开发数据变体）",
        2,
    )
    cards = (
        (
            0.55,
            "A · 供应商断供",
            "关键供应商取消供应",
            [
                "任务 2280：在既有销售承诺、预算与毛利下替换采购、重排制造。",
                "判断哪些承诺还能保留，哪些计划必须修改或取消。",
            ],
        ),
        (
            6.25,
            "B · 产线停机",
            "关键工作中心不可用",
            [
                "任务 2299：受影响制造计划必须取消并重排。",
                "不能占用停机工作中心，也不能虚构产能或到货。",
            ],
        ),
    )
    for x, label, title, body in cards:
        _add_rect(slide, x, 1.55, 5.85, 2.55, fill=SOFT, line=LINE, radius=0.06)
        _add_rect(slide, x, 1.55, 0.075, 2.55, fill=ACCENT, line=None, radius=None,
                  shape=MSO_SHAPE.RECTANGLE)
        _kicker_label(slide, x + 0.3, 1.76, label)
        _add_text(slide, x + 0.3, 2.05, 5.2, 0.45, title, size=19, bold=True)
        _add_bullets(slide, x + 0.3, 2.62, 5.2, 1.3, body, size=10.5, gap=7)

    _add_text(slide, 0.55, 4.4, 4.5, 0.3, "共同约束", size=10, bold=True, color=MUTED)
    chips = ["授权范围", "每单预算", "毛利口径", "交期", "异常身份"]
    x = 0.55
    for chip in chips:
        width = 1.35 if len(chip) <= 4 else 1.65
        _add_rect(slide, x, 4.72, width, 0.46, fill=WHITE, line=LINE, radius=0.5)
        _add_text(slide, x, 4.72, width, 0.46, chip, size=9.5, color=INK,
                  align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        x += width + 0.18

    _add_text(
        slide,
        0.55,
        5.52,
        7.5,
        0.3,
        "验收程序独立于 Agent；业务是否通过，由外部原始验收器判定。",
        size=10.5,
        color=INK,
    )
    _add_rect(slide, 0.55, 5.95, 12.2, 0.62, fill=ACCENT_SOFT, line=None, radius=0.05)
    _add_text(
        slide,
        0.78,
        5.95,
        11.7,
        0.62,
        "第一版只处理这两个开发场景，不扩展到复合异常、全厂 APS 或真实工厂系统。",
        size=10,
        bold=True,
        color=ACCENT,
        anchor=MSO_ANCHOR.MIDDLE,
    )


def _slide_why_hard(prs) -> None:
    slide = _content_slide(
        prs,
        "为什么难：失败不在“不会写代码”，而在计划语义",
        "三类真实失败，暴露三个不同层次的问题",
        "证据：recovery-b-off-20261020-01 / matched-b-native-off-20261020-01 / workflow-b-off-20261020-01 · 配置：planning-metadata-v1",
        3,
    )
    items = (
        (
            "01",
            "目标态 ≠ 增量",
            "求解器返回的是完整目标状态。直接逐条 create，会保留旧单又新建全部目标量，产生重复采购甚至无依据动作。",
        ),
        (
            "02",
            "工具可用 ≠ 会被使用",
            "B 的 74 次工具调用全部是 bash，prepare / execute 为 0 次。Agent 绕过受控工具自行改 ERP，最终 21 分、7 项规则失败。",
        ),
        (
            "03",
            "通过 ≠ 政策正确",
            "B 最终用销售单未税金额 883,693.80 代替任务书预算 943,302.00。这次更紧且通过，但不能说政策提取正确。",
        ),
    )
    for index, (number, title, body) in enumerate(items):
        y = 1.55 + index * 1.58
        _add_rect(slide, 0.55, y, 12.2, 1.38, fill=SOFT if index % 2 == 0 else WHITE,
                  line=LINE, radius=0.05)
        _add_number_badge(slide, 0.82, y + 0.34, 0.62, number, size=11)
        _add_text(slide, 1.68, y + 0.18, 3.2, 0.36, title, size=15, bold=True)
        _add_text(slide, 1.68, y + 0.62, 10.55, 0.62, body, size=10.5, color=INK)

    _add_text(
        slide,
        0.55,
        6.36,
        12.2,
        0.35,
        "结论：必须把“计划真实性”从模型文本，移到执行器、合同和可复核的证据里。",
        size=11.5,
        bold=True,
        color=ACCENT,
    )


def _slide_solution(prs) -> None:
    slide = _content_slide(
        prs,
        "总体方案：一条可审计的受控执行链",
        "Agent 负责理解与求解；执行器负责写；原始验收器负责判分",
        "实现：Pi 0.85.1 · Nemotron-3.5-Super-VL · Harbor 0.24.0 · Odoo 19.0 · CP-SAT · 配置：planning-metadata-v1（业务运行）",
        4,
    )
    steps = (
        ("01", "读取", "任务书与实时 Odoo；只读调查，不直接写。"),
        ("02", "prepare", "采集快照、重新求解、生成 keep/update/cancel/create 变更集。"),
        ("03", "绑定", "plan_id、政策摘要、首次基线与审批绑定。"),
        ("04", "execute", "固定写入、journal 持久化、逐步回读。"),
        ("05", "外部验收", "原始规则评分；strict_success 另行判定。"),
    )
    width = 2.28
    for index, (number, title, body) in enumerate(steps):
        x = 0.55 + index * (width + 0.2)
        _add_rect(slide, x, 1.55, width, 2.65, fill=SOFT, line=LINE, radius=0.06)
        _add_number_badge(slide, x + 0.2, 1.76, 0.55, number, size=10.5)
        _add_text(slide, x + 0.2, 2.47, width - 0.4, 0.3, title, size=14, bold=True)
        _add_text(slide, x + 0.2, 2.88, width - 0.4, 1.1, body, size=9.5, color=MUTED)
        if index < len(steps) - 1:
            _add_arrow(slide, x + width + 0.02, 2.86, x + width + 0.18, 2.86,
                       color=ACCENT, width=1.2)

    _add_text(slide, 0.55, 4.58, 5.0, 0.3, "三条硬约束", size=10, bold=True, color=MUTED)
    principles = (
        ("不直接持有写凭证", "Agent 只有只读 key；写入经 root 执行器。"),
        ("不把目标量当新增量", "变更集映射真实单据，避免重复建单。"),
        ("不把工具回读当成功", "外部原始验收器给出业务结论。"),
    )
    for index, (title, body) in enumerate(principles):
        x = 0.55 + index * 4.12
        _add_rect(slide, x, 4.95, 3.9, 1.32, fill=WHITE, line=LINE, radius=0.05)
        _add_rect(slide, x, 4.95, 0.06, 1.32, fill=ACCENT, line=None, radius=None,
                  shape=MSO_SHAPE.RECTANGLE)
        _add_text(slide, x + 0.24, 5.12, 3.45, 0.3, title, size=12, bold=True)
        _add_text(slide, x + 0.24, 5.5, 3.45, 0.6, body, size=9.5, color=MUTED)

    _add_text(slide, 0.55, 6.48, 12.2, 0.3,
              "同一套内核，两种模式：基准模式预授权自主执行；产品模式在 execute 前增加人工审批。",
              size=9.5, color=MUTED)


def _slide_architecture(prs) -> None:
    slide = _content_slide(
        prs,
        "架构图：Agent、root 执行器与独立验收",
        "写入只有一条路径：经 Unix socket 进入 root 执行器",
        "证据：docs/P1_权限拆分与政策合同.md §4 · 配置：permission-split-v1",
        5,
    )
    # lane labels
    for x, label in ((0.55, "任务侧"), (2.1, "Agent 侧（非 root）"),
                     (2.1, "执行器侧（root）"), (7.7, "验收侧（独立）")):
        _add_text(slide, x, 1.5 if label != "执行器侧（root）" else 4.18,
                  2.6, 0.22, label, size=8, bold=True, color=MUTED)

    # top row
    _add_rect(slide, 0.55, 1.82, 1.25, 1.35, fill=SOFT, line=LINE, radius=0.07)
    _add_text(slide, 0.55, 1.95, 1.25, 1.1, "任务书", size=12, bold=True,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    _add_text(slide, 0.55, 2.55, 1.25, 0.5, "业务请求", size=8.5, color=MUTED,
              align=PP_ALIGN.CENTER)

    _add_rect(slide, 2.1, 1.82, 2.3, 1.35, fill=WHITE, line=ACCENT, width=1.25, radius=0.07)
    _add_text(slide, 2.3, 1.95, 1.9, 0.6, "Pi + Nemotron", size=13, bold=True)
    _add_text(slide, 2.3, 2.42, 1.9, 0.55, "只读调查\nbash / read", size=8.5, color=MUTED)

    _add_rect(slide, 4.95, 1.82, 2.2, 3.95, fill=SOFT_2, line=LINE, radius=0.05)
    _add_text(slide, 5.15, 2.0, 1.8, 0.35, "Odoo ERP", size=13, bold=True)
    _add_text(slide, 5.15, 2.42, 1.8, 1.2, "实时计划\n与单据", size=9.5, color=MUTED)
    _add_text(slide, 5.15, 3.28, 1.8, 1.3, "只读 key\n固定写入\n回读", size=8.5, color=MUTED)

    _add_rect(slide, 7.7, 1.82, 2.35, 1.38, fill=WHITE, line=LINE, radius=0.07)
    _add_text(slide, 7.9, 1.96, 1.95, 0.35, "原始验收器", size=12.5, bold=True)
    _add_text(slide, 7.9, 2.34, 1.95, 0.72, "ERP-Bench 规则\n独立于模型", size=8.5, color=MUTED)

    # bottom row
    _add_rect(slide, 2.1, 4.52, 2.3, 1.25, fill=WHITE, line=ACCENT, width=1.25, radius=0.07)
    _add_text(slide, 2.3, 4.61, 1.9, 0.32, "root 执行器", size=12.5, bold=True)
    _add_text(slide, 2.3, 4.98, 1.9, 0.68, "prepare · apply\njournal · readback", size=8, color=MUTED)

    _add_rect(slide, 7.7, 4.52, 2.35, 1.25, fill=WHITE, line=LINE, radius=0.07)
    _add_text(slide, 7.9, 4.64, 1.95, 0.32, "Ed25519 attestation", size=11, bold=True)
    _add_text(slide, 7.9, 5.0, 1.95, 0.6, "对 verifier 产物签名\n状态：absent / verified / invalid",
              size=7.8, color=MUTED)

    _add_rect(slide, 10.7, 2.55, 2.05, 2.4, fill=DARK, line=None, radius=0.06)
    _add_text(slide, 10.9, 2.72, 1.7, 0.35, "RunView", size=13, bold=True, color=WHITE)
    _add_text(slide, 10.9, 3.12, 1.65, 0.75, "只读投影\n运行台 / 产品 API", size=8.5,
              color=RGBColor(0xC8, 0xCE, 0xD6))
    _add_text(slide, 10.9, 4.05, 1.65, 0.55, "分数、规则、执行\n与缺测分开", size=8,
              color=RGBColor(0x9C, 0xA5, 0xB1))

    # arrows
    _add_arrow(slide, 1.82, 2.49, 2.08, 2.49, width=1.2)
    _add_arrow(slide, 4.42, 2.45, 4.93, 2.45, width=1.2)
    _add_text(slide, 4.05, 2.16, 0.85, 0.22, "只读 key", size=7.5, color=ACCENT)
    _add_arrow(slide, 3.25, 3.19, 3.25, 4.5, width=1.4)
    _add_text(slide, 3.38, 3.48, 1.25, 0.45, "Unix socket\nplan_id", size=7.5, color=MUTED)
    _add_arrow(slide, 4.42, 5.14, 4.93, 5.14, width=1.2)
    _add_text(slide, 4.02, 5.22, 0.95, 0.22, "固定写入", size=7.5, color=ACCENT)
    _add_arrow(slide, 7.17, 2.5, 7.68, 2.5, width=1.2)
    _add_text(slide, 7.2, 2.2, 0.55, 0.22, "终态", size=7.5, color=MUTED)
    _add_arrow(slide, 8.88, 3.22, 8.88, 4.5, width=1.2)
    _add_text(slide, 9.0, 3.55, 0.8, 0.22, "签名", size=7.5, color=MUTED)
    _add_arrow(slide, 10.07, 2.5, 10.68, 3.1, width=1.2)
    _add_arrow(slide, 10.07, 5.1, 10.68, 4.45, width=1.2)

    _add_text(
        slide,
        0.55,
        6.0,
        12.2,
        0.62,
        "权限边界：Agent 无 admin key；执行器只接受受控请求；验收器加固并以签名绑定产物，但仍不宣称“不可篡改”。",
        size=9.5,
        color=MUTED,
    )


def _slide_permissions(prs) -> None:
    slide = _content_slide(
        prs,
        "权限边界：Agent 只读，root 执行器唯一可写",
        "P1 用容器内特权分离，把“能写”从模型进程拿走",
        "证据：permission-split-probe-20261021-10 · 配置：permission-split-v1（data=original，nop 权限探针）",
        6,
    )
    _add_rect(slide, 0.55, 1.52, 7.25, 2.15, fill=SOFT, line=LINE, radius=0.05)
    _add_pill(slide, 0.78, 1.7, 0.95, 0.3, "非 root", fill=DARK, size=8)
    _add_text(slide, 1.9, 1.68, 4.0, 0.34, "Agent 用户 forgeflow", size=15, bold=True)
    _add_bullets(
        slide,
        0.8,
        2.13,
        6.7,
        1.4,
        [
            "可读：容器内 /etc/odoo/api_key.read（0644 root:forgeflow）",
            "可写：无。直接写 Odoo 返回 403；模型不能自行授予写权限",
            "职责：调查、读取业务数据、经 socket 调用 prepare / apply",
        ],
        size=10.5,
        gap=6,
    )

    _add_rect(slide, 0.55, 3.85, 7.25, 2.55, fill=WHITE, line=ACCENT, width=1.15, radius=0.05)
    _add_pill(slide, 0.78, 4.03, 1.55, 0.3, "唯一写路径", fill=ACCENT, size=8)
    _add_text(slide, 2.5, 4.01, 4.4, 0.34, "root 执行器", size=15, bold=True)
    _add_bullets(
        slide,
        0.8,
        4.46,
        6.7,
        1.75,
        [
            "写 key：容器内 /etc/odoo/api_key（0600 root），Agent 不可读",
            "socket：/run/forgeflow/executor.sock（0660），SO_PEERCRED 校验 uid",
            "store：/var/lib/forgeflow/recovery（0700 root），Agent 无法遍历",
            "apply 需要 plan_id 与审批；不接受任意计划 JSON",
        ],
        size=10.5,
        gap=5,
    )

    _add_rect(slide, 8.1, 1.52, 4.65, 4.88, fill=SOFT, line=LINE, radius=0.05)
    _kicker_label(slide, 8.35, 1.7, "容器探针 · permission-split-probe-20261021-10", w=4.1)
    metrics = (
        ("200", "ro_read"),
        ("403", "ro_write"),
        ("600", "api_key_mode"),
        ("644", "read_key_mode"),
    )
    for index, (value, label) in enumerate(metrics):
        x = 8.35 + (index % 2) * 2.1
        y = 2.08 + (index // 2) * 1.05
        _add_rect(slide, x, y, 1.9, 0.85, fill=WHITE, line=LINE, radius=0.05)
        _add_text(slide, x + 0.12, y + 0.08, 1.65, 0.4, value, size=23, bold=True, color=ACCENT)
        _add_text(slide, x + 0.12, y + 0.52, 1.65, 0.24, label, size=8, color=MUTED)

    _add_text(slide, 8.35, 4.32, 4.1, 0.3, "selfcheck=ok · nop 对照 0 分", size=9.5,
              bold=True)
    _add_text(slide, 8.35, 4.68, 4.1, 0.3, "evaluation_valid=true", size=9.5, bold=True)
    _add_text(
        slide,
        8.35,
        5.08,
        4.1,
        0.85,
        "只读用户能读、不能写；admin key 已移出 Agent 可读范围。\ndata=original 仅用于权限探针，不评价业务。",
        size=8.5,
        color=MUTED,
    )
    _add_text(slide, 8.35, 5.95, 4.1, 0.3, "结论：admin key 不在 Agent 进程可读范围内。",
              size=9, bold=True, color=ACCENT)


def _slide_contract(prs) -> None:
    slide = _content_slide(
        prs,
        "任务政策合同：政策不能由模型“复述”",
        "TaskContract 从任务书、场景数据与已记录请求构造；不读取 reference solution",
        "证据：workflow-b-off-20261020-01（历史请求复核）/ contracts/2299-workcenter-outage-v1.json · 配置：planning-metadata-v1",
        7,
    )
    _add_rect(slide, 0.55, 1.52, 6.9, 4.1, fill=SOFT, line=LINE, radius=0.05)
    _add_text(slide, 0.8, 1.7, 5.8, 0.3, "合同覆盖什么", size=11, bold=True, color=MUTED)
    fields = (
        ("产品 / 锚点日期", "目标产品与任务基准日"),
        ("订单范围", "授权的销售单集合，缺一或多一都拒绝"),
        ("每单预算", "逐单上限；更紧的预算也算冲突"),
        ("毛利口径", "按售价口径校验，不做静默替换"),
        ("异常身份", "供应商引用或停机工作中心"),
        ("原承诺策略", "保留可执行承诺 / 修复受影响计划"),
    )
    for index, (title, body) in enumerate(fields):
        x = 0.8 + (index % 2) * 3.25
        y = 2.08 + (index // 2) * 1.1
        _add_text(slide, x, y, 2.95, 0.28, title, size=10.5, bold=True, color=ACCENT)
        _add_text(slide, x, y + 0.31, 2.95, 0.55, body, size=9, color=INK)

    _add_text(
        slide,
        0.8,
        5.12,
        6.35,
        0.24,
        "校验两处：prepare 前的 request；execute 前的 change-set。",
        size=8.5,
        color=MUTED,
    )
    _add_text(
        slide,
        0.8,
        5.37,
        6.35,
        0.24,
        "物理动作在结构上不可能：执行器只接受 10 种计划单据动作。",
        size=8.5,
        color=MUTED,
    )

    _add_rect(slide, 7.75, 1.52, 5.0, 4.1, fill=WHITE, line=ACCENT, width=1.2, radius=0.05)
    _kicker_label(slide, 8.0, 1.7, "真实拦截")
    _add_text(slide, 8.0, 1.98, 1.2, 0.8, "12", size=46, bold=True, color=ACCENT)
    _add_text(slide, 9.0, 2.12, 3.4, 0.3, "个预算字段被拒绝", size=13, bold=True)
    _add_text(slide, 9.0, 2.5, 3.4, 0.3, "合同拒绝发生在任何写入之前", size=8.5, color=MUTED)
    _add_rule(slide, 8.0, 3.05, 4.5, color=LINE)
    _add_text(slide, 8.0, 3.2, 4.3, 0.27, "B 记录值（销售单未税金额）", size=8.5, color=MUTED)
    _add_text(slide, 8.0, 3.47, 4.3, 0.36, "883,693.80", size=18, bold=True)
    _add_text(slide, 8.0, 3.98, 4.3, 0.27, "任务书预算", size=8.5, color=MUTED)
    _add_text(slide, 8.0, 4.25, 4.3, 0.36, "943,302.00", size=18, bold=True)
    _add_text(
        slide,
        8.0,
        4.78,
        4.35,
        0.7,
        "更紧也不等于正确；不能把“碰巧通过”当成政策提取正确。",
        size=8.5,
        color=MUTED,
    )
    _add_text(slide, 7.75, 5.85, 5.0, 0.3, "政策摘要进入 prepare 与审批绑定。",
              size=9, bold=True, color=ACCENT)


def _slide_recovery(prs) -> None:
    slide = _content_slide(
        prs,
        "恢复计划与变更集：把目标态翻译成“动哪些单”",
        "prepare 在实时 Odoo 上重算；执行只做已经绑定的增量",
        "证据：permission-split-pi-20261021-09 / workflow-a-off-20261020-01 / workflow-b-off-20261020-01 · 配置：planning-metadata-v1 + permission-split-v1",
        8,
    )
    _add_text(slide, 0.55, 1.48, 4.0, 0.3, "变更集", size=10, bold=True, color=MUTED)
    quadrants = (
        ("KEEP", "保留", "仍可执行的承诺与单据"),
        ("UPDATE", "修改", "日期、数量、关联关系"),
        ("CANCEL", "取消", "受影响的制造计划"),
        ("CREATE", "新建", "必要的采购 / 制造单"),
    )
    for index, (code, title, body) in enumerate(quadrants):
        x = 0.55 + (index % 2) * 3.55
        y = 1.82 + (index // 2) * 1.52
        _add_rect(slide, x, y, 3.3, 1.32, fill=SOFT, line=LINE, radius=0.05)
        _add_text(slide, x + 0.2, y + 0.15, 2.9, 0.24, code, size=8, bold=True, color=ACCENT)
        _add_text(slide, x + 0.2, y + 0.42, 2.9, 0.3, title, size=13, bold=True)
        _add_text(slide, x + 0.2, y + 0.8, 2.9, 0.35, body, size=9, color=MUTED)

    _add_text(slide, 7.85, 1.48, 4.6, 0.3, "执行证据链", size=10, bold=True, color=MUTED)
    _add_rect(slide, 7.85, 1.82, 4.85, 2.85, fill=WHITE, line=LINE, radius=0.05)
    steps = (
        "快照与实时业务状态",
        "baseline / binding：首次基线",
        "plan_id + policy 摘要",
        "journal.jsonl：持久化操作顺序",
        "readback / success：逐项回读",
    )
    for index, text in enumerate(steps):
        y = 1.98 + index * 0.5
        _add_rect(slide, 8.08, y + 0.08, 0.16, 0.16, fill=ACCENT, line=None,
                  shape=MSO_SHAPE.OVAL, radius=None)
        _add_text(slide, 8.38, y, 4.05, 0.28, text, size=9, color=INK)

    _add_text(
        slide,
        7.85,
        4.82,
        4.85,
        0.3,
        "P1 导出 complete=true；逐文件大小与 SHA 记录。",
        size=8,
        color=MUTED,
    )
    _add_text(
        slide,
        0.55,
        5.05,
        12.2,
        0.3,
        "真实执行：同一 plan_id 下固定写入，并保留执行日志与回读。",
        size=10,
        bold=True,
        color=MUTED,
    )
    metrics = (
        ("7 步 · 13 文件", "P1：verified_steps / 导出恢复产物"),
        ("7 步 · 11 写", "Workflow A：计划单据写入"),
        ("34 步 · 36 写", "Workflow B：计划单据写入"),
    )
    for index, (value, label) in enumerate(metrics):
        x = 0.55 + index * 4.1
        _add_rect(slide, x, 5.42, 3.9, 0.92, fill=SOFT, line=LINE, radius=0.05)
        _add_text(slide, x + 0.23, 5.55, 3.45, 0.32, value, size=15, bold=True, color=ACCENT)
        _add_text(slide, x + 0.23, 5.94, 3.45, 0.24, label, size=8, color=MUTED)

    _add_text(
        slide,
        0.55,
        6.52,
        12.2,
        0.3,
        "不生成无实物依据的收货、生产完工或库存调整；执行响应不确定时保留原计划。",
        size=9,
        color=MUTED,
    )


def _slide_approval(prs) -> None:
    slide = _content_slide(
        prs,
        "审批与故障语义：一次性闸门，未知结果不盲重试",
        "产品模式先审批；基准模式预授权，但共用同一执行内核",
        "证据：permission-split-pi-20261021-09（recovery-route）· 配置：permission-split-v1 + planning-metadata-v1",
        9,
    )
    _add_rect(slide, 0.55, 1.52, 6.0, 3.6, fill=SOFT, line=LINE, radius=0.05)
    _kicker_label(slide, 0.8, 1.7, "审批")
    _add_text(slide, 0.8, 1.98, 4.8, 0.38, "一次性、可绑定、可恢复", size=16, bold=True)
    _add_bullets(
        slide,
        0.8,
        2.5,
        5.45,
        2.6,
        [
            "绑定 run / plan / policy / snapshot / approver / 过期时间",
            "O_EXCL 消费，只闸首次 dispatch；重放与对账不需要新令牌",
            "过期或绑定不符：写入前拒绝，失败不消耗令牌",
            "apply 必须使用同一 plan_id；前端不能改计划 JSON",
        ],
        size=10,
        gap=7,
    )
    _add_text(
        slide,
        0.8,
        4.55,
        5.45,
        0.42,
        "审批后状态变化则拒绝旧计划；恢复路径仍检查过期与绑定。",
        size=8,
        color=MUTED,
    )

    _add_rect(slide, 6.75, 1.52, 6.0, 3.6, fill=WHITE, line=ACCENT, width=1.1, radius=0.05)
    _kicker_label(slide, 7.0, 1.7, "故障")
    _add_text(slide, 7.0, 1.98, 4.8, 0.38, "未知结果先对账，不重发", size=16, bold=True)
    flow = ("响应未知", "保留 plan / journal", "核对回读", "同一 plan 对账")
    for index, text in enumerate(flow):
        x = 7.0 + index * 1.38
        _add_rect(slide, x, 2.63, 1.25, 0.58, fill=SOFT, line=LINE, radius=0.08)
        _add_text(slide, x, 2.63, 1.25, 0.58, text, size=7.5, color=INK,
                  align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        if index < len(flow) - 1:
            _add_arrow(slide, x + 1.27, 2.92, x + 1.36, 2.92, width=1)
    _add_bullets(
        slide,
        7.0,
        3.43,
        5.45,
        1.7,
        [
            "禁止自动重发、重新 prepare 覆盖原计划",
            "崩溃恢复只允许同一 plan_id；不提供未验证的“一键回滚”",
            "停止生成文字不等于撤销已经发生的 ERP 写入",
        ],
        size=9.5,
        gap=6,
    )
    _add_text(
        slide,
        7.0,
        4.62,
        5.45,
        0.35,
        "恢复不是重做：已有 dispatch 的日志跳过首次闸门，按 journal 对账。",
        size=8,
        color=MUTED,
    )

    _add_text(slide, 0.55, 5.38, 12.2, 0.3, "两种模式，同一内核", size=10, bold=True,
              color=MUTED)
    modes = (
        ("基准模式", "预授权自主执行，保留完整失败与对照记录；不增加人工响应。"),
        ("产品模式", "展示变更集 → 人工批准 → 同一 plan_id 执行 → 外部验收；Web 写路径待接。"),
        ("真实链路", "apply 发起与 ok 均记录在 recovery-route.jsonl；非 Agent 直写。"),
    )
    for index, (title, body) in enumerate(modes):
        x = 0.55 + index * 4.1
        _add_rect(slide, x, 5.7, 3.9, 0.9, fill=SOFT, line=LINE, radius=0.05)
        _add_text(slide, x + 0.18, 5.8, 3.55, 0.22, title, size=8.5, bold=True, color=ACCENT)
        _add_text(slide, x + 0.18, 6.05, 3.55, 0.48, body, size=7.5, color=INK)


def _slide_attestation(prs) -> None:
    slide = _content_slide(
        prs,
        "验收与防篡改：原始规则判分，签名绑定产物",
        "工具回读通过只是执行证据；业务是否通过由外部原始验收器决定",
        "证据：attestation-nop-20261021-01 / permission-split-pi-20261021-09 · 配置：permission-split-v1（nop 对照 data=original）+ planning-metadata-v1",
        10,
    )
    columns = (
        (
            "01",
            "原始验收",
            [
                "ERP-Bench 的 test.sh、checks.py、reward.json 保留原样。",
                "ForgeFlow 只适配 Harbor 0.24.0 的分数读取，不改业务规则。",
                "strict_success 还要求所有适用规则通过、链路有效。",
                "evaluation_valid 与 strict_success 分开记录。",
            ],
        ),
        (
            "02",
            "验收器加固",
            [
                "verifier-hardened-v1 注入以下变量：",
                "PYTHONSAFEPATH=1",
                "PYTHONPATH=\"\" · UV_NO_CONFIG=1",
                "BASH_ENV=/dev/null",
                "nop 对照 0 分、evaluation_valid=true。",
                "仍不能证明验收时工作区一定干净。",
            ],
        ),
        (
            "03",
            "产物签名",
            [
                "Ed25519 对 verifier 产物签名；RunView 显示 absent / verified / invalid。",
                "attestation-nop-20261021-01：7 个 artifacts，status=verified。",
                "100 分运行已签名并验签通过。",
                "签名失败只记录 failed，不改分数与状态。",
            ],
        ),
    )
    for index, (number, title, body) in enumerate(columns):
        x = 0.55 + index * 4.1
        _add_rect(slide, x, 1.52, 3.9, 4.35, fill=SOFT if index != 1 else WHITE,
                  line=LINE, radius=0.05)
        _add_number_badge(slide, x + 0.22, 1.72, 0.5, number, size=9.5)
        _add_text(slide, x + 0.24, 2.38, 3.4, 0.35, title, size=15, bold=True)
        _add_bullets(slide, x + 0.24, 2.85, 3.42, 2.8, body, size=9, gap=7)

    _add_rect(slide, 0.55, 6.02, 12.2, 0.62, fill=ACCENT_SOFT, line=None, radius=0.05)
    _add_text(
        slide,
        0.78,
        6.02,
        11.75,
        0.62,
        "边界：签名证明产物在签名后未被改动，不能证明验收时工作区干净；因此不宣称“结果不可篡改”。",
        size=9.5,
        bold=True,
        color=ACCENT,
        anchor=MSO_ANCHOR.MIDDLE,
    )


def _slide_results(prs) -> None:
    slide = _content_slide(
        prs,
        "真实运行结果：A 3/3、B 2/3 在权限拆分下满分",
        "六次真实运行（A/B 各 3 次），全部经执行器与原始验收",
        "证据：A …-09 / p5-A-perm-pi-r2..r3 · B …-b-01 / p5-B-perm-pi-r2..r3",
        11,
    )
    _add_rect(slide, 0.55, 1.52, 4.3, 3.95, fill=DARK, line=None, radius=0.06)
    _add_text(slide, 0.82, 1.75, 3.7, 0.28, "原始验收分数", size=9, bold=True, color=ACCENT)
    _add_text(slide, 0.78, 1.92, 3.8, 1.0, "100.0", size=60, bold=True, color=WHITE)
    _add_text(slide, 0.82, 3.02, 3.7, 0.3, "A 51/51 · B 95/95 规则通过", size=11.5,
              color=WHITE)
    _add_text(slide, 0.82, 3.42, 3.7, 0.28, "两个场景 0 失败规则", size=9.5,
              color=RGBColor(0xC8, 0xCE, 0xD6))
    _add_text(slide, 0.82, 3.68, 3.7, 0.28, "strict_success = true", size=9.5,
              color=RGBColor(0xC8, 0xCE, 0xD6))
    _add_text(slide, 0.82, 3.85, 3.7, 0.28, "evaluation_valid = true", size=9.5,
              color=RGBColor(0xC8, 0xCE, 0xD6))
    _add_pill(slide, 0.82, 4.82, 1.55, 0.34, "3 次重复", fill=ACCENT, size=8)
    _add_text(slide, 2.52, 4.85, 2.0, 0.28, "不称稳定成功率", size=8, color=FAINT)

    _add_text(slide, 5.2, 1.52, 2.0, 0.28, "运行配置", size=9, bold=True, color=MUTED)
    rows = (
        ("run_id", "A …-09 / B …-b-01"),
        ("task", "A 供应商断供 / B 产线停机"),
        ("model", "nvidia/Nemotron-3.5-Super-VL"),
        ("planner", "cp-sat-recovery-v1"),
        ("data", "planning-metadata-v1"),
        ("permission", "permission-split-v1"),
    )
    for index, (key, value) in enumerate(rows):
        y = 1.86 + index * 0.48
        _add_text(slide, 5.2, y, 1.1, 0.24, key, size=8, color=FAINT)
        _add_text(slide, 6.3, y - 0.02, 6.3, 0.28, value, size=9.5, bold=True)
        if index < len(rows) - 1:
            _add_rule(slide, 5.2, y + 0.38, 7.35, color=LINE)

    _add_rect(slide, 5.2, 4.85, 7.45, 0.62, fill=SOFT, line=LINE, radius=0.05)
    _add_text(slide, 5.4, 4.94, 7.05, 0.24,
              "执行器 uid 0 · socket 0660 · store 0700 · 7 步验证 · 13 个导出文件",
              size=8.5, color=INK)
    _add_text(slide, 5.4, 5.19, 7.05, 0.22,
              "recovery-route：prepare / apply 均为 executor 模式。",
              size=7.5, color=MUTED)

    _add_rect(slide, 0.55, 5.72, 12.2, 0.92, fill=ACCENT_SOFT, line=None, radius=0.05)
    _add_text(
        slide,
        0.78,
        5.82,
        11.7,
        0.7,
        "口径：A 3/3 满分；B 2/3 满分，失败的一次是模型上下文溢出（recovery-route 为空，未触达执行器）。"
        "均为 planning-metadata-v1 + permission-split-v1；不代表 original 条件、稳定成功率或保留任务。",
        size=8.5,
        color=ACCENT,
    )


def _slide_matrix(prs) -> None:
    slide = _content_slide(
        prs,
        "实验矩阵：分组报告，不合并口径",
        "同一模型与任务链下，工具、提示与权限是分别标识的实验因素",
        "证据：见下表 run_id · 配置：第 5 行 planning-metadata-v1；第 6 行 planning-metadata-v1 + permission-split-v1",
        12,
    )
    rows = [
        ("组别 / 目的", "关键配置", "A 断供", "B 停线", "证据 ID"),
        ("参考执行（oracle）", "原始环境对照", "100", "100（95/95）",
         "m1-a-oracle-20261020-03 / m1-b-oracle-20261020-01"),
        ("空操作（nop）", "原始环境对照", "0", "0",
         "m1-a-nop-20261020-01 / m1-b-nop-20261020-01"),
        ("Pi V0", "原始数据，无恢复工具", "0", "0",
         "v0-a-pi-20261020-02 / v0-b-pi-20261020-01"),
        ("只读求解对照", "cp-sat-native-v1", "17.5", "21.33",
         "matched-a-native-off-20261020-01 / matched-b-native-off-20261020-01"),
        ("恢复 + 工作流", "cp-sat-recovery-v1 + recovery-workflow-v1", "100（51/51）",
         "100（95/95）", "workflow-a-off-20261020-01 / workflow-b-off-20261020-01"),
        ("权限拆分 P1", "planning-metadata-v1 + permission-split-v1 + cp-sat-recovery-v1",
         "3/3 满分", "2/3 满分",
         "…-09 + p5-A-perm-pi-r2..r3 / …-b-01 + p5-B-perm-pi-r2..r3"),
    ]
    table_shape = slide.shapes.add_table(
        len(rows), 5, Inches(0.55), Inches(1.48), Inches(12.2), Inches(4.65)
    )
    table = table_shape.table
    widths = (2.0, 3.25, 1.35, 1.55, 4.05)
    for index, width in enumerate(widths):
        table.columns[index].width = Inches(width)
    heights = (0.45, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7)
    for index, height in enumerate(heights):
        table.rows[index].height = Inches(height)
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            cell = table.cell(row_index, col_index)
            cell.fill.solid()
            if row_index == 0:
                cell.fill.fore_color.rgb = DARK
            elif row_index == 6:
                cell.fill.fore_color.rgb = ACCENT_SOFT
            else:
                cell.fill.fore_color.rgb = WHITE if row_index % 2 else SOFT
            cell.margin_left = Inches(0.08)
            cell.margin_right = Inches(0.06)
            cell.margin_top = Inches(0.03)
            cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            frame = cell.text_frame
            frame.word_wrap = True
            paragraph = frame.paragraphs[0]
            paragraph.alignment = PP_ALIGN.LEFT
            run = paragraph.add_run()
            run.text = value
            _style_run(
                run,
                size=8.5 if row_index else 9,
                bold=row_index == 0 or (col_index == 0 and row_index == 6),
                color=WHITE if row_index == 0 else INK,
            )
    _add_text(
        slide,
        0.55,
        6.3,
        12.2,
        0.5,
        "第 1–5 行为历史对照，第 6 行为本轮 3 次重复（A 3/3、B 2/3）；两组配置不合并成功率。"
        "所有行均为开发场景样本，不据此推断稳定泛化。",
        size=8.5,
        color=MUTED,
    )


def _slide_product(prs) -> None:
    slide = _content_slide(
        prs,
        "产品界面：先把证据看清楚，再谈对话",
        "首版控制台已含运行、计划、审批与项目问答；assistant-ui 多轮对话仍在计划中",
        "证据：permission-split-pi-20261021-09 的 RunView 投影 · 配置：planning-metadata-v1 + permission-split-v1",
        13,
    )
    _add_rect(slide, 0.55, 1.52, 7.45, 4.95, fill=SOFT, line=LINE, radius=0.05)
    _add_pill(slide, 0.8, 1.7, 2.2, 0.3, "已实现 · 首版控制台", fill=DARK, size=8)
    _add_text(slide, 0.8, 2.1, 6.9, 0.35, "RunView schema 1", size=15, bold=True)
    sections = (
        ("运行与证据", "分数、规则、evaluation_valid、签名状态、数据 profile"),
        ("计划与变更集", "plan_id、操作计数、步数、预期支出、policy 摘要"),
        ("人工审批", "绑定审阅者与计划；一次性、可撤销、写审计日志"),
        ("项目问答", "只返回文档原文段落与 path:start-end 引用"),
    )
    for index, (title, body) in enumerate(sections):
        y = 2.58 + index * 0.85
        _add_rect(slide, 0.8, y, 6.95, 0.7, fill=WHITE, line=LINE, radius=0.04)
        _add_text(slide, 0.98, y + 0.1, 1.4, 0.24, title, size=9.5, bold=True, color=ACCENT)
        _add_text(slide, 2.42, y + 0.1, 5.1, 0.42, body, size=8.8, color=INK)
    _add_text(
        slide,
        0.8,
        6.02,
        6.9,
        0.3,
        "GET / 单页控制台；接口只读运行与审批记录，不触发模型或 ERP 写入；不展示思考文本与凭证。",
        size=7.8,
        color=MUTED,
    )

    _add_rect(slide, 8.25, 1.52, 4.5, 4.95, fill=WHITE, line=ACCENT, width=1.0, radius=0.05)
    _add_pill(slide, 8.48, 1.7, 2.6, 0.3, "规划中 · assistant-ui 多轮", fill=ACCENT, size=8)
    _add_text(slide, 8.48, 2.1, 4.0, 0.3, "未实现，不计入本次成绩", size=8.5,
              bold=True, color=MUTED)
    columns = (
        ("左", "场景 / 运行记录"),
        ("中", "对话 + 审批入口"),
        ("右", "来源 / 计划 / 结果"),
    )
    for index, (title, body) in enumerate(columns):
        y = 2.62 + index * 0.8
        _add_rect(slide, 8.48, y, 4.0, 0.62, fill=SOFT, line=LINE, radius=0.05)
        _add_text(slide, 8.65, y + 0.08, 0.35, 0.3, title, size=10, bold=True, color=ACCENT)
        _add_text(slide, 9.05, y + 0.12, 3.2, 0.3, body, size=9, color=INK)
    _add_bullets(
        slide,
        8.48,
        5.12,
        4.0,
        1.1,
        [
            "项目讲解只读；制造任务显式进入",
            "复用 WayRaft 布局 + assistant-ui 消息",
            "模型文字不能直接变成可执行按钮",
        ],
        size=8,
        gap=3,
    )
    _add_text(
        slide,
        0.55,
        6.6,
        12.2,
        0.25,
        "以上为结构示意，不是真实截图；模拟事件不计作实验成果。",
        size=7.5,
        color=FAINT,
    )


def _slide_deploy(prs) -> None:
    slide = _content_slide(
        prs,
        "部署与安全：本地只读已闭环，公网入口尚未完成",
        "把“研究入口”和“评审产品入口”分开，不用界面替代业务可靠性",
        "证据：permission-split-probe-20261021-10 / README 运行边界 · 配置：permission-split-v1",
        14,
    )
    columns = (
        (
            "已实现",
            "本地只读闭环",
            [
                "运行台只绑定回环地址，不暴露公网",
                "只读 RunView API；无模型 / ERP 写入口",
                "只读凭证 + root 执行器 + 验收器加固",
                "Ed25519 签名；RunView 显示 verified",
                "P1 收尾记录：1061 项测试通过、1 项跳过",
            ],
            "done",
        ),
        (
            "待完成 · P6",
            "评审入口",
            [
                "HTTPS 产品入口与评审身份",
                "任务所有权、单任务排队与健康检查",
                "独立 Odoo 初态与 Worker 隔离",
                "不向浏览器暴露 Odoo、数据库、Docker socket、原始日志目录",
            ],
            "todo",
        ),
        (
            "红线",
            "产品化不能突破",
            [
                "密钥不进源码、日志或演示材料",
                "没有验收结果就不显示任务成功",
                "资源不可用时显示失败 / 排队，不回退成伪实时",
                "未经验证的“一键回滚”不做",
            ],
            "rule",
        ),
    )
    for index, (tag, title, body, kind) in enumerate(columns):
        x = 0.55 + index * 4.1
        line = ACCENT if kind == "done" else LINE
        _add_rect(slide, x, 1.52, 3.9, 3.35, fill=WHITE if kind != "done" else SOFT,
                  line=line, width=1.1 if kind == "done" else 0.75, radius=0.05)
        _add_pill(slide, x + 0.22, 1.72, 1.25, 0.3, tag,
                  fill=ACCENT if kind == "done" else SOFT_2,
                  color=WHITE if kind == "done" else MUTED, size=8)
        _add_text(slide, x + 0.22, 2.18, 3.45, 0.3, title, size=15, bold=True)
        _add_bullets(slide, x + 0.22, 2.65, 3.45, 2.0, body, size=9.2, gap=6)

    _add_rect(slide, 0.55, 5.12, 6.0, 0.88, fill=SOFT, line=LINE, radius=0.05)
    _add_text(slide, 0.78, 5.22, 5.55, 0.22, "已实测", size=8, bold=True, color=ACCENT)
    _add_text(slide, 0.78, 5.47, 5.55, 0.4,
              "permission-split-probe-20261021-10：0 分、evaluation_valid=true。",
              size=8, color=INK)
    _add_rect(slide, 6.75, 5.12, 6.0, 0.88, fill=WHITE, line=LINE, radius=0.05)
    _add_text(slide, 6.98, 5.22, 5.55, 0.22, "未完成", size=8, bold=True, color=ACCENT)
    _add_text(slide, 6.98, 5.47, 5.55, 0.4,
              "外网部署、评审账号、匿名检查、产品写路径。",
              size=8, color=INK)

    _add_rect(slide, 0.55, 6.22, 12.2, 0.5, fill=ACCENT_SOFT, line=None, radius=0.05)
    _add_text(
        slide,
        0.78,
        6.22,
        11.75,
        0.5,
        "部署原则：真实运行与历史回放分开；没有结果不显示成功；服务不可用就明确显示失败或排队。",
        size=8.8,
        color=ACCENT,
        anchor=MSO_ANCHOR.MIDDLE,
    )


def _slide_limits(prs) -> None:
    slide = _content_slide(
        prs,
        "限制与反思：哪些结论现在还不能下",
        "开发场景有重复、保留任务有反例：两者都必须如实报告",
        "证据：permission-split-pi-20261021-09 / 工程进度 2026-10-21 · 配置：planning-metadata-v1（非 original）",
        15,
    )
    cards = (
        ("01", "样本范围", "开发场景 A 3/3、B 2/3；保留任务 2281 一次 0 分，被原型边界拒绝。"),
        ("02", "数据条件", "结果使用 planning-metadata-v1，它是可见性变体，不是 original；不声称原始数据条件通过。"),
        ("03", "政策提取", "B 仍从销售单金额取预算；本次更紧且通过，不等于任务政策提取正确。"),
        ("04", "时间粒度", "任务按日核算；同日小时 / 秒级先后仍有缺口，不是精确工厂排程。"),
        ("05", "工具覆盖", "只支持有限的采购 / 制造计划；多工序、多 BoM、部分履约、多币种或单位会被拒绝。"),
        ("06", "成本与产品", "美元成本未返回，保持 null；产品审批与本地部署已可用，公网与完整工作台未完成。"),
    )
    for index, (number, title, body) in enumerate(cards):
        x = 0.55 + (index % 3) * 4.1
        y = 1.52 + (index // 3) * 2.12
        _add_rect(slide, x, y, 3.9, 1.9, fill=SOFT if index % 2 == 0 else WHITE,
                  line=LINE, radius=0.05)
        _add_text(slide, x + 0.22, y + 0.15, 0.5, 0.24, number, size=8, bold=True, color=ACCENT)
        _add_text(slide, x + 0.22, y + 0.42, 3.4, 0.3, title, size=12.5, bold=True)
        _add_text(slide, x + 0.22, y + 0.85, 3.45, 0.9, body, size=9.2, color=MUTED)

    _add_text(
        slide,
        0.55,
        6.2,
        12.2,
        0.3,
        "下一步优先级：先补重复与保留任务，再补工作台与部署；不以界面完成替代业务可靠性。",
        size=9.5,
        bold=True,
        color=ACCENT,
    )


def _slide_roadmap(prs) -> None:
    slide = _content_slide(
        prs,
        "路线图：P0–P6，先把真实性与权限做完",
        "已完成项按证据标记；计划项不写成已完成",
        "依据：ForgeFlow_下一阶段开发方案_v3.md §13 / 工程进度 2026-10-21 / permission-split-pi-20261021-09 · 配置：planning-metadata-v1 + permission-split-v1（P1）",
        16,
    )
    phases = (
        ("P0", "冻结 + RunView", "完成", "基线提交与读取投影；历史结果可复现展示。"),
        ("P1", "权限 / 合同 / 审批", "关键验证", "签名与执行器已接入；权限拆分首次 100 分。"),
        ("P2", "产品 API", "完成首版", "运行创建/队列、计划、审批/撤销/审计、SSE、知识问答；已容器验证。"),
        ("P3", "控制台", "完成首版", "无构建单页：运行、计划、审批、多轮问答、新建运行；assistant-ui 待迁移。"),
        ("P4", "知识库与问答", "完成", "BM25 检索、原文与行号引用；接口与界面已接。"),
        ("P5", "重复 / 保留任务", "首轮完成", "A 3/3、B 2/3；保留任务 2281 一次 0 分（原型拒绝）。"),
        ("P6", "部署 / 安全 / 材料", "完成首版", "本地 compose 实测、18 页材料、匿名化清单；公网未部署。"),
    )
    width = 1.66
    for index, (phase, title, status, body) in enumerate(phases):
        x = 0.55 + index * (width + 0.1)
        complete = status in {"完成", "完成首版", "首轮完成"}
        fill = DARK if complete else (SOFT if status in {"关键验证", "部分"} else WHITE)
        text_color = WHITE if complete else INK
        muted_color = RGBColor(0xB9, 0xC1, 0xCC) if complete else MUTED
        border = None if complete else (ACCENT if status in {"关键验证", "部分"} else LINE)
        _add_rect(slide, x, 1.55, width, 3.35, fill=fill, line=border, radius=0.05)
        _add_text(slide, x + 0.14, 1.7, 0.7, 0.3, phase, size=13, bold=True,
                  color=WHITE if complete else ACCENT)
        _add_text(slide, x + 0.14, 2.08, width - 0.28, 0.42, title, size=9.5, bold=True,
                  color=text_color)
        _add_text(slide, x + 0.14, 2.65, width - 0.28, 0.22, status, size=7.5, bold=True,
                  color=ACCENT if not complete else RGBColor(0xF0, 0x9A, 0x78))
        _add_text(slide, x + 0.14, 3.0, width - 0.28, 1.7, body, size=7.5, color=muted_color)

    _add_text(slide, 0.55, 5.2, 12.2, 0.3, "下一步顺序", size=9, bold=True, color=MUTED)
    _add_text(
        slide,
        0.55,
        5.52,
        12.2,
        0.4,
        "assistant-ui 迁移  →  保留任务能力扩展  →  公网部署与匿名检查  →  更大样本重复",
        size=10.5,
        bold=True,
        color=INK,
    )
    _add_text(
        slide,
        0.55,
        6.1,
        12.2,
        0.5,
        "成功门槛：原始验收、strict_success、证据链、权限边界同时满足；"
        "资源不足时先保权限与真实性，削减页面美化。",
        size=8.5,
        color=MUTED,
    )


def _slide_team(prs) -> None:
    slide = _content_slide(
        prs,
        "团队 / 分工：只写可核实的贡献，不补身份",
        "国内展示不列学校、姓名、导师；仓库也未记录团队人数",
        "来源：README.md / THIRD_PARTY_NOTICES.md / 工程进度 · 本页不提供身份信息",
        17,
    )
    columns = (
        (
            "工程与验证",
            [
                "任务接入、恢复工具、变更集、执行器",
                "合同、审批、签名与 RunView",
                "真实运行、失败复盘与证据索引",
                "项目维护者（不列姓名）",
            ],
        ),
        (
            "上游与依赖",
            [
                "ERP-Bench：任务、Odoo 与原始验收",
                "Pi 0.85.1 / Harbor 0.24.0",
                "OR-Tools CP-SAT",
                "Nebius Token Factory / NVIDIA Nemotron",
            ],
        ),
        (
            "材料与边界",
            [
                "README、工程进度、P1 文档、首版证据",
                "所有数字绑定 run_id；未测保持未知",
                "团队名单 / 外部协作：仓库未记录，材料不编造",
            ],
        ),
    )
    for index, (title, items) in enumerate(columns):
        x = 0.55 + index * 4.1
        _add_rect(slide, x, 1.52, 3.9, 2.9, fill=SOFT, line=LINE, radius=0.05)
        _add_number_badge(slide, x + 0.22, 1.72, 0.45, f"0{index + 1}", size=8.5)
        _add_text(slide, x + 0.8, 1.75, 2.8, 0.3, title, size=14, bold=True)
        _add_bullets(slide, x + 0.22, 2.28, 3.45, 1.8, items, size=9.5, gap=8)

    _add_rect(slide, 0.55, 4.72, 6.0, 1.3, fill=WHITE, line=LINE, radius=0.05)
    _add_text(slide, 0.78, 4.86, 5.5, 0.24, "已记录", size=8, bold=True, color=ACCENT)
    _add_text(slide, 0.78, 5.15, 5.55, 0.72,
              "工程实现、真实运行、失败复盘、证据索引、上游复用边界。",
              size=9, color=INK)
    _add_rect(slide, 6.75, 4.72, 6.0, 1.3, fill=WHITE, line=LINE, radius=0.05)
    _add_text(slide, 6.98, 4.86, 5.5, 0.24, "未记录", size=8, bold=True, color=ACCENT)
    _add_text(slide, 6.98, 5.15, 5.55, 0.72,
              "团队人数、学校、姓名、导师、外部协作；材料不补写身份。",
              size=9, color=INK)

    _add_rect(slide, 0.55, 6.22, 12.2, 0.5, fill=ACCENT_SOFT, line=None, radius=0.05)
    _add_text(
        slide,
        0.8,
        6.28,
        11.7,
        0.4,
        "本页不提供学校、姓名、导师或团队人数。身份信息只在赛事系统要求处单独提交，"
        "不写进作品正文、截图或文件元数据。",
        size=9.5,
        bold=True,
        color=ACCENT,
        anchor=MSO_ANCHOR.MIDDLE,
    )


def _slide_end(prs) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, DARK)
    _add_rect(slide, 0.7, 1.55, 0.09, 2.2, fill=ACCENT, line=None, radius=None,
              shape=MSO_SHAPE.RECTANGLE)
    _add_text(slide, 1.02, 1.4, 9.5, 0.8, "ForgeFlow", size=44, bold=True, color=WHITE)
    _add_text(slide, 1.04, 2.35, 10.0, 0.55,
              "让每一次生产异常处置，都能追到计划、写入与验收。", size=20, color=WHITE)
    _add_text(slide, 1.04, 3.1, 10.0, 0.4,
              "谢谢 · Q&A", size=15, bold=True, color=ACCENT)
    _add_rule(slide, 1.04, 4.2, 11.3, color=DARK_LINE)
    values = (
        ("真实任务", "供应商断供 / 产线停机"),
        ("原始验收", "ERP-Bench 规则，不由模型判分"),
        ("受控写入", "只读 Agent + root 执行器"),
    )
    for index, (title, body) in enumerate(values):
        x = 1.04 + index * 3.75
        _add_text(slide, x, 4.45, 3.4, 0.26, title, size=9, bold=True, color=ACCENT)
        _add_text(slide, x, 4.76, 3.45, 0.3, body, size=10, color=WHITE)
    _add_text(
        slide,
        1.04,
        6.35,
        10.8,
        0.35,
        "所有结果绑定 run_id；未测量项保持未知。A 3/3、B 2/3；保留任务一次 0 分。",
        size=8.5,
        color=FAINT,
    )
    _page(slide, 18, dark=True)


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def build_deck() -> Presentation:
    """Return the 18-slide presentation."""
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT
    props = prs.core_properties
    props.title = "ForgeFlow 首版演示"
    props.subject = "制造业生产与交付异常处置智能体"
    props.author = "ForgeFlow"
    props.last_modified_by = "ForgeFlow"
    props.comments = "18 页中文首版；结果绑定 run_id；未测量项保持未知。"
    props.keywords = f"ForgeFlow; 制造业; 异常处置; {CJK_FONT}; {CJK_FALLBACK}"
    props.created = datetime(2026, 10, 21, 0, 0, 0)
    props.modified = datetime(2026, 10, 21, 0, 0, 0)
    props.revision = 1

    _slide_cover(prs)
    _slide_problem(prs)
    _slide_why_hard(prs)
    _slide_solution(prs)
    _slide_architecture(prs)
    _slide_permissions(prs)
    _slide_contract(prs)
    _slide_recovery(prs)
    _slide_approval(prs)
    _slide_attestation(prs)
    _slide_results(prs)
    _slide_matrix(prs)
    _slide_product(prs)
    _slide_deploy(prs)
    _slide_limits(prs)
    _slide_roadmap(prs)
    _slide_team(prs)
    _slide_end(prs)
    return prs


def _normalise_package(payload: io.BytesIO, target: Path) -> None:
    """Write the package with fixed zip metadata for reproducible bytes."""
    with zipfile.ZipFile(payload) as source:
        names = source.namelist()
        ordered = ["[Content_Types].xml"]
        ordered.extend(sorted(name for name in names if name != "[Content_Types].xml"))
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for name in ordered:
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 21, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0o600 << 16
                output.writestr(info, source.read(name), compress_type=zipfile.ZIP_DEFLATED,
                                compresslevel=9)


def write_deck(out: Path) -> Path:
    """Build and atomically replace ``out``; return the written path."""
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = io.BytesIO()
    build_deck().save(payload)
    payload.seek(0)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".forgeflow-deck-", suffix=".pptx", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        _normalise_package(payload, temporary_path)
        with temporary_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o644)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 ForgeFlow 首版 18 页 PPT")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"输出路径（默认 {DEFAULT_OUT}）")
    args = parser.parse_args(argv)
    path = write_deck(args.out)
    print(f"wrote {path} ({SLIDE_COUNT} slides)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
