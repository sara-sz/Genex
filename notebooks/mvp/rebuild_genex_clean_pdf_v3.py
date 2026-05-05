
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)

def _safe(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v)

def _normalize_payloads(obj: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    def walk(x: Any):
        if isinstance(x, dict):
            out.append(x)
        elif isinstance(x, list):
            for item in x:
                walk(item)
    walk(obj)
    return out

def _chunk(seq: List[Any], n: int) -> List[List[Any]]:
    return [seq[i:i+n] for i in range(0, len(seq), n)]

def _find_transcript_lines(payload: Dict[str, Any]) -> List[str]:
    candidates = [
        payload.get("question_answer_transcript"),
        payload.get("parent_input_lines"),
        payload.get("transcript_lines"),
        payload.get("transcript"),
        payload.get("qa_transcript"),
    ]
    for c in candidates:
        if isinstance(c, list):
            return [str(x) for x in c]
        if isinstance(c, str) and c.strip():
            return [line.strip() for line in c.splitlines() if line.strip()]

    state = payload.get("state", {})
    for key in ["question_answer_transcript", "parent_input_lines", "transcript_lines", "transcript", "qa_transcript"]:
        c = state.get(key)
        if isinstance(c, list):
            return [str(x) for x in c]
        if isinstance(c, str) and c.strip():
            return [line.strip() for line in c.splitlines() if line.strip()]

    return []

def _find_summary_rows(payload: Dict[str, Any]) -> List[List[str]]:
    rows = [["Domain", "Dev age", "Gap", "Tier"]]
    summary_df = payload.get("summary_df")
    if summary_df is not None:
        try:
            for _, r in summary_df.iterrows():
                rows.append([
                    _safe(r.get("category")),
                    _safe(r.get("estimated_dev_age_months")),
                    _safe(r.get("milestone_gap_months")),
                    _safe(r.get("support_tier")),
                ])
            if len(rows) > 1:
                return rows
        except Exception:
            pass

    state = payload.get("state", {})
    summaries = state.get("domain_summaries") or state.get("category_summaries") or []
    for r in summaries:
        rows.append([
            _safe(r.get("category") or r.get("display")),
            _safe(r.get("estimated_dev_age_months") or r.get("dev_age_months")),
            _safe(r.get("milestone_gap_months") or r.get("gap_months")),
            _safe(r.get("support_tier") or r.get("tier")),
        ])
    return rows if len(rows) > 1 else [["Domain", "Dev age", "Gap", "Tier"]]

def _find_focus_areas(payload: Dict[str, Any]) -> List[str]:
    for container in [payload, payload.get("state", {})]:
        fa = container.get("focus_areas")
        if isinstance(fa, list) and fa:
            return [str(x) for x in fa]
    return []

def _find_activities(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    for container in [payload, payload.get("state", {})]:
        acts = container.get("daily_activities") or container.get("activities") or container.get("weekly_activities")
        if isinstance(acts, list) and acts:
            return acts
    return []

def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Heading1"], fontSize=15.5, leading=18, spaceAfter=8, textColor=colors.HexColor("#1f3b5b")),
        "case_title": ParagraphStyle("case_title", parent=base["Heading2"], fontSize=13, leading=15, spaceAfter=6, textColor=colors.HexColor("#183153")),
        "section": ParagraphStyle("section", parent=base["Heading3"], fontSize=10.5, leading=12, spaceBefore=3, spaceAfter=3, textColor=colors.HexColor("#183153")),
        "normal": ParagraphStyle("normal", parent=base["BodyText"], fontSize=8.3, leading=10.2, spaceAfter=2, alignment=TA_LEFT),
        "small": ParagraphStyle("small", parent=base["BodyText"], fontSize=7.1, leading=8.5, spaceAfter=1.5, alignment=TA_LEFT),
        "tiny": ParagraphStyle("tiny", parent=base["BodyText"], fontSize=6.5, leading=7.6, spaceAfter=1.0, alignment=TA_LEFT),
    }

def _one_col_box(flowables: List[Any], width: float, border_color: str):
    tbl = Table([[f] for f in flowables], colWidths=[width])
    tbl.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 0.8, colors.HexColor(border_color)),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    return tbl

def build_clean_case_report_pdf(all_payloads: List[Dict[str, Any]], out_pdf: str = "genex_case_report_clean.pdf") -> str:
    styles = _styles()
    out_pdf = str(Path(out_pdf).expanduser())
    doc = SimpleDocTemplate(
        out_pdf,
        pagesize=letter,
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
    )
    story: List[Any] = [Paragraph("Genex Advisor Review Packet - Clean Layout", styles["title"]), Spacer(1, 6)]

    for i, payload in enumerate(all_payloads):
        state = payload.get("state", {})
        child_name = payload.get("child_name") or state.get("child_name") or state.get("name") or f"Case {i+1}"
        case_id = payload.get("case_id") or state.get("case_id") or f"C{i+1:02d}"
        age = _safe(payload.get("chronological_age_months") or state.get("chronological_age_months"))
        diagnosis = _safe(payload.get("diagnosis") or state.get("diagnosis"), "No diagnosis")
        concern = _safe(payload.get("concern") or state.get("concern"))
        daily_time = _safe(payload.get("daily_time_min") or state.get("daily_time_min") or 10)

        story.append(Paragraph(f"{case_id} - {child_name}", styles["case_title"]))

        profile_data = [
            ["Name", child_name, "Age (months)", age],
            ["Diagnosis", diagnosis, "Daily time", f"{daily_time} min/day"],
            ["Concern", concern, "", ""],
        ]
        profile = Table(profile_data, colWidths=[0.85*inch, 2.65*inch, 1.0*inch, 1.6*inch])
        profile.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#eaf1fb")),
            ("BOX", (0,0), (-1,-1), 0.7, colors.HexColor("#6f8fb8")),
            ("INNERGRID", (0,0), (-1,-1), 0.35, colors.HexColor("#9db4d3")),
            ("SPAN", (1,2), (3,2)),
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("LEADING", (0,0), (-1,-1), 10),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 5),
            ("RIGHTPADDING", (0,0), (-1,-1), 5),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(profile)
        story.append(Spacer(1, 6))

        # Parent input in chunks to avoid overflow
        story.append(Paragraph("2. Parent Input (questions + answers)", styles["section"]))
        transcript_lines = _find_transcript_lines(payload)
        if not transcript_lines:
            transcript_lines = ["Full transcript preserved in JSON output."]
        for chunk in _chunk(transcript_lines[:24], 6):
            html = "<br/>".join("• " + line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for line in chunk)
            story.append(_one_col_box([Paragraph(html, styles["tiny"])], 6.55*inch, "#6f8fb8"))
            story.append(Spacer(1, 4))

        # Genex output
        story.append(Paragraph("3. Genex Output", styles["section"]))
        summary_rows = _find_summary_rows(payload)
        genex_tbl = Table(summary_rows, colWidths=[2.0*inch, 0.7*inch, 0.6*inch, 2.2*inch])
        genex_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#eef5ee")),
            ("BOX", (0,0), (-1,-1), 0.7, colors.HexColor("#88a388")),
            ("INNERGRID", (0,0), (-1,-1), 0.35, colors.HexColor("#b7c8b7")),
            ("FONTSIZE", (0,0), (-1,-1), 7.5),
            ("LEADING", (0,0), (-1,-1), 9),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING", (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ]))
        story.append(genex_tbl)
        focus = _find_focus_areas(payload)
        if focus:
            focus_html = "<b>Focus areas</b><br/>" + "<br/>".join("• " + x for x in focus)
            story.append(Spacer(1, 3))
            story.append(Paragraph(focus_html, styles["normal"]))
        story.append(Spacer(1, 6))

        # Activities: one box per activity
        story.append(Paragraph("4. Daily Activities", styles["section"]))
        acts = _find_activities(payload)
        if not acts:
            story.append(_one_col_box([Paragraph("No activity list found in payload.", styles["small"])], 6.55*inch, "#9c7d4f"))
        else:
            for idx, a in enumerate(acts[:5], start=1):
                title = _safe(a.get("title") or a.get("name") or a.get("activity"), f"Activity {idx}")
                mins = _safe(a.get("minutes") or a.get("duration_min"), "5")
                goal = _safe(a.get("goal") or a.get("support_tier") or a.get("tier"), "")
                desc = _safe(a.get("description") or a.get("instructions") or "")
                html = f"<b>{idx}. {title} ({mins} min)</b> - {goal}<br/>{desc}"
                story.append(_one_col_box([Paragraph(html, styles["small"])], 6.55*inch, "#9c7d4f"))
                story.append(Spacer(1, 4))
        story.append(Spacer(1, 4))

        summary_text = _safe(payload.get("summary") or state.get("summary") or "")
        if not summary_text:
            summary_text = f"This plan prioritizes {', '.join(focus) if focus else 'the identified focus areas'} for {child_name}. The activities are intended to be short, parent-friendly, and aligned with the developmental areas that appeared most delayed or most worth enriching in the interview."

        story.append(Paragraph("5. Summary", styles["section"]))
        story.append(_one_col_box([Paragraph(summary_text, styles["normal"])], 6.55*inch, "#9aa3ad"))
        story.append(Spacer(1, 4))

        review_text = (
            "Rate 1-5: Clinical appropriateness | Safety | Practicality for parents | "
            "Clarity | Overall usefulness<br/>"
            "Short feedback: What would you change? What is missing? Any concerns?"
        )
        story.append(Paragraph("6. Advisor Review", styles["section"]))
        story.append(_one_col_box([Paragraph(review_text, styles["normal"])], 6.55*inch, "#9aa3ad"))

        if i < len(all_payloads) - 1:
            story.append(PageBreak())

    doc.build(story)
    return out_pdf

def build_clean_case_report_from_json_dir(json_dir: str, out_pdf: str = "genex_case_report_clean.pdf") -> str:
    json_dir = Path(json_dir)
    payloads: List[Dict[str, Any]] = []
    for p in sorted(json_dir.glob("*.json")):
        try:
            obj = json.loads(p.read_text())
            payloads.extend(_normalize_payloads(obj))
        except Exception:
            pass
    if not payloads:
        raise ValueError(f"No readable JSON payloads found in {json_dir}")
    return build_clean_case_report_pdf(payloads, out_pdf=out_pdf)
