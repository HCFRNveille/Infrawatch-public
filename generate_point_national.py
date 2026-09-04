#!/usr/bin/env python3
"""
INFRAWATCH — Générateur du Point de situation national.

Architecture :
  données/scoring InfraWatch -> fact_packet déterministe
  -> LLM analytique (prose uniquement)
  -> validation
  -> JSON final déterministe
  -> PDF + HTML

Le LLM ne reçoit aucun droit de modifier les niveaux N0-N4.
Les niveaux et chiffres sont injectés dans le document après l'analyse.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether
)

PARIS = ZoneInfo("Europe/Paris")
SECTOR_ORDER = ["electricity", "nuclear", "gas", "fuel", "telecom", "rail"]
SECTOR_LABELS = {
    "electricity": "Électricité",
    "nuclear": "Nucléaire",
    "gas": "Gaz",
    "fuel": "Carburants",
    "telecom": "Télécommunications",
    "rail": "Ferroviaire",
}
LEVEL_LABELS = {
    "N0": "Nominal",
    "N1": "Vigilance",
    "N2": "Dégradé",
    "N3": "Critique",
    "N4": "Rupture",
    "ND": "Données insuffisantes",
}

DEFAULT_PUBLIC_BASE = "https://hcfrnveille.github.io/Infrawatch-public"
FILES = [
    "dashboard.json",
    "latest.json",
    "latest_live.json",
    "health.json",
    "national_status.json",
    "risks_threats.json",
]


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def fetch_json(url: str, timeout: int = 20) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": "InfraWatch-Point-National/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def load_sources(source_dir: Path, public_base: str) -> tuple[dict[str, Any], dict[str, str]]:
    data: dict[str, Any] = {}
    origins: dict[str, str] = {}

    for name in FILES:
        local = read_json(source_dir / name)
        if local is not None:
            data[name] = local
            origins[name] = "local"
            continue

        remote = fetch_json(f"{public_base.rstrip('/')}/{name}")
        if remote is not None:
            data[name] = remote
            origins[name] = "public_http"

    return data, origins


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def freshness_minutes(value: Any, now: datetime) -> int | None:
    dt = parse_dt(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((now.astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() // 60))


def current_cycle(now_paris: datetime, forced: str | None) -> str:
    if forced:
        normalized = forced.upper().replace(":", "").replace("H", "")
        if normalized in {"0800", "08"}:
            return "08H00"
        if normalized in {"1700", "17"}:
            return "17H00"
        raise ValueError("Cycle forcé invalide. Utiliser 08H00 ou 17H00.")
    return "08H00" if now_paris.hour < 13 else "17H00"


def validate_core_sources(sources: dict[str, Any]) -> None:
    required = ["dashboard.json", "latest_live.json"]
    missing = [name for name in required if name not in sources]
    if missing:
        raise RuntimeError(f"Sources InfraWatch indispensables absentes : {', '.join(missing)}")

    dashboard = sources["dashboard.json"]
    latest = sources["latest_live.json"]

    if not isinstance(dashboard.get("sectors"), list):
        raise RuntimeError("dashboard.json: sectors absent ou invalide.")

    for sid in SECTOR_ORDER:
        if sid == "fuel":
            continue
        latest_key = "telecom_mobile" if sid == "telecom" else sid
        if sid == "rail":
            latest_key = "rail"
        if latest_key not in latest:
            raise RuntimeError(f"latest_live.json: secteur {latest_key} absent.")

    if "fuels" not in latest:
        raise RuntimeError("latest_live.json: secteur fuels absent.")


def canonical_sector_map(dashboard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    aliases = {
        "fuels": "fuel",
        "telecom_mobile": "telecom",
    }
    result: dict[str, dict[str, Any]] = {}
    for sector in dashboard.get("sectors", []):
        if not isinstance(sector, dict):
            continue
        sid = aliases.get(str(sector.get("id")), str(sector.get("id")))
        result[sid] = copy.deepcopy(sector)
    return result


def compact_history(dashboard: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "history_7d": [
            {"label": row.get("label"), "value": _history_value(row, key)}
            for row in dashboard.get("history", [])
        ],
        "history_24h": [
            {"label": row.get("label"), "value": _history_value(row, key)}
            for row in dashboard.get("live_history", [])
        ],
    }


def _history_value(row: dict[str, Any], key: str) -> Any:
    if key == "fuel":
        return row.get("fuel")
    if key == "telecom":
        return row.get("telecom")
    if key == "electricity":
        value = (row.get("electricity") or {}).get("forecast_error_pct")
        return abs(value) if isinstance(value, (int, float)) else None
    if key == "nuclear":
        return (row.get("nuclear") or {}).get("fleet_availability_pct")
    if key == "gas":
        return (row.get("gas") or {}).get("operational_status")
    if key == "rail":
        return (row.get("rail") or {}).get("canceled_ratio_pct")
    return None


def current_metrics(latest: dict[str, Any], sid: str) -> dict[str, Any]:
    if sid == "fuel":
        f = latest.get("fuels") or {}
        return {
            "eligible_stations": f.get("eligible_stations"),
            "stations_with_any_shortage": f.get("stations_with_any_shortage"),
            "national_shortage_ratio_pct": f.get("national_shortage_ratio_pct"),
            "departments_with_shortage": f.get("departments_with_shortage"),
            "top_departments": f.get("top_departments") or f.get("departments") or [],
        }
    if sid == "telecom":
        t = latest.get("telecom_mobile") or {}
        return {
            "national_down_ratio_pct": t.get("national_down_ratio_pct"),
            "unique_operator_sites_down": t.get("unique_operator_sites_down"),
            "four_operator_clusters": t.get("four_operator_clusters"),
            "top_departments": t.get("top_departments") or [],
        }
    if sid == "rail":
        trip = (latest.get("rail") or {}).get("trip_updates") or {}
        return {
            "trips_count": trip.get("trips_count"),
            "canceled_trips": trip.get("canceled_trips"),
            "canceled_ratio_pct": trip.get("canceled_ratio_pct"),
            "trips_delay_ge_30min_ratio_pct": trip.get("trips_delay_ge_30min_ratio_pct"),
            "trips_delay_ge_60min_ratio_pct": trip.get("trips_delay_ge_60min_ratio_pct"),
            "service_alerts": (latest.get("rail") or {}).get("service_alerts") or {},
        }
    if sid == "electricity":
        e = latest.get("electricity") or {}
        return {
            "consumption_mw": e.get("consumption_mw"),
            "forecast_j_mw": e.get("forecast_j_mw"),
            "forecast_j1_mw": e.get("forecast_j1_mw"),
            "forecast_error_pct": e.get("forecast_error_pct"),
            "physical_exchanges_mw": e.get("physical_exchanges_mw"),
            "exchange_ratio_pct": e.get("exchange_ratio_pct"),
            "production_total_mw": e.get("production_total_mw"),
            "production_by_fuel_mw": e.get("production_by_fuel_mw") or {},
        }
    if sid == "nuclear":
        n = latest.get("nuclear") or {}
        return {
            "fleet_max_capacity_mw": n.get("fleet_max_capacity_mw"),
            "fleet_available_capacity_mw": n.get("fleet_available_capacity_mw"),
            "fleet_unavailable_capacity_mw": n.get("fleet_unavailable_capacity_mw"),
            "fleet_availability_pct": n.get("fleet_availability_pct"),
            "planned_current_events": n.get("planned_current_events"),
            "unplanned_current_events": n.get("unplanned_current_events"),
            "chronic_current_events": n.get("chronic_current_events"),
            "current_operational_unavailability_events": n.get("current_operational_unavailability_events"),
        }
    if sid == "gas":
        g = latest.get("gas") or {}
        op = g.get("operational") or {}
        ref = g.get("consumption_reference") or {}
        return {
            "operational_status": op.get("operational_status"),
            "data_quality": op.get("data_quality"),
            "limits_count": op.get("limits_count"),
            "green_limits_count": op.get("green_limits_count"),
            "orange_limits_count": op.get("orange_limits_count"),
            "red_limits_count": op.get("red_limits_count"),
            "violet_limits_count": op.get("violet_limits_count"),
            "unknown_limits_count": op.get("unknown_limits_count"),
            "alerts": op.get("alerts") or [],
            "limits": op.get("limits") or [],
            "consumption_reference": {
                "freshness": ref.get("freshness"),
                "france_total_mw": ref.get("france_total_mw"),
                "natran_mw": ref.get("natran_mw"),
                "terega_mw": ref.get("terega_mw"),
            },
        }
    return {}


def build_fact_packet(sources: dict[str, Any], origins: dict[str, str], cycle: str, now: datetime) -> dict[str, Any]:
    dashboard = sources["dashboard.json"]
    latest = sources["latest_live.json"]
    sector_map = canonical_sector_map(dashboard)

    sectors: dict[str, Any] = {}
    for sid in SECTOR_ORDER:
        backend = sector_map.get(sid, {})
        sectors[sid] = {
            "label": SECTOR_LABELS[sid],
            "official_level": backend.get("level", "ND"),
            "backend_trend": backend.get("trend"),
            "backend_metric": backend.get("metric"),
            "backend_detail": backend.get("detail"),
            "current": current_metrics(latest, sid),
            **compact_history(dashboard, sid),
        }

    generated_at = dashboard.get("generated_at") or latest.get("generated_at")
    packet = {
        "schema_version": "point-national-facts-1.0",
        "cycle": cycle,
        "generated_at": generated_at,
        "generated_freshness_minutes": freshness_minutes(generated_at, now),
        "source_origins": origins,
        "doctrine": {
            "scoring_authority": "InfraWatch backend only",
            "llm_role": "qualitative analysis only",
            "no_frontend_or_llm_scoring": True,
            "no_automatic_causality": True,
        },
        "national": dashboard.get("national") or {},
        "driver": dashboard.get("driver") or {},
        "source_health": dashboard.get("source_health") or {},
        "sectors": sectors,
        "territories": dashboard.get("territories") or [],
        "correlations": dashboard.get("correlations") or [],
        "risks_threats": dashboard.get("threats") or sources.get("risks_threats.json") or {},
    }
    return packet


def load_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8")


def call_llm(
    fact_packet: dict[str, Any],
    prompt: str,
    schema: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """
    Uses OpenAI Responses API when OPENAI_API_KEY is present.
    The model returns prose-only JSON; official levels are NOT part of the model schema.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Package `openai` manquant. Installer les requirements.") from exc

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Secret OPENAI_API_KEY absent.")

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": prompt,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "output_schema": schema,
                        "fact_packet": fact_packet,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
    )
    raw = response.output_text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Réponse LLM non JSON : {raw[:500]}") from exc


def validate_analysis(analysis: dict[str, Any]) -> None:
    required = {
        "national_synthesis",
        "sector_summaries",
        "intersectoral_dynamics",
        "surveillance_points",
        "national_assessment",
    }
    if set(analysis.keys()) != required:
        raise RuntimeError(f"Schéma LLM invalide. Clés reçues : {sorted(analysis.keys())}")

    summaries = analysis.get("sector_summaries")
    if not isinstance(summaries, dict) or set(summaries.keys()) != set(SECTOR_ORDER):
        raise RuntimeError("sector_summaries incomplet ou invalide.")

    text_parts = [
        analysis["national_synthesis"],
        analysis["national_assessment"],
        *summaries.values(),
        *[str(item.get("analysis", "")) for item in analysis["intersectoral_dynamics"]],
        *[str(item.get("analysis", "")) for item in analysis["surveillance_points"]],
    ]

    # Hard doctrine guard: the model is not allowed to emit an N-level code.
    forbidden = re.compile(r"\bN[0-4]\b", flags=re.IGNORECASE)
    for block in text_parts:
        if forbidden.search(str(block)):
            raise RuntimeError(
                "Garde-fou doctrinal : le LLM a émis un code N0-N4. "
                "Document refusé pour éviter toute ambiguïté avec le moteur."
            )


def build_final_document(facts: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "point-national-document-1.0",
        "cycle": facts["cycle"],
        "generated_at": facts["generated_at"],
        "national": facts["national"],
        "driver": facts["driver"],
        "source_health": facts["source_health"],
        "national_synthesis": analysis["national_synthesis"],
        "sectors": {
            sid: {
                "label": facts["sectors"][sid]["label"],
                "official_level": facts["sectors"][sid]["official_level"],
                "backend_trend": facts["sectors"][sid]["backend_trend"],
                "current": facts["sectors"][sid]["current"],
                "analysis": analysis["sector_summaries"][sid],
            }
            for sid in SECTOR_ORDER
        },
        "intersectoral_dynamics": analysis["intersectoral_dynamics"],
        "surveillance_points": analysis["surveillance_points"],
        "national_assessment": analysis["national_assessment"],
        "doctrine": facts["doctrine"],
    }


def fmt_level(level: str) -> str:
    return f"{level} — {LEVEL_LABELS.get(level, level)}"


def safe_text(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(document: dict[str, Any], output: Path) -> None:
    rows = []
    for sid in SECTOR_ORDER:
        s = document["sectors"][sid]
        rows.append(
            f"<section class='sector'><h2>{safe_text(s['label'])} "
            f"<span>{safe_text(fmt_level(s['official_level']))}</span></h2>"
            f"<p>{safe_text(s['analysis'])}</p></section>"
        )

    watches = "".join(
        f"<li><strong>{safe_text(item['title'])}</strong> — {safe_text(item['analysis'])}</li>"
        for item in document["surveillance_points"]
    )
    dynamics = "".join(
        f"<li><strong>{safe_text(item['title'])}</strong> "
        f"<em>{safe_text(item['status'])}</em> — {safe_text(item['analysis'])}</li>"
        for item in document["intersectoral_dynamics"]
    )

    html = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>InfraWatch — Point national {safe_text(document['cycle'])}</title>
<style>
body{{font-family:Arial,sans-serif;background:#07131b;color:#dbe7ec;margin:0}}
main{{max-width:1050px;margin:auto;padding:32px}}
header{{border-left:4px solid #ff9a42;padding:18px;background:#0c1b24;border-top:1px solid #29404c;border-right:1px solid #29404c;border-bottom:1px solid #29404c}}
h1{{margin:5px 0;font-size:28px}} .eyebrow{{letter-spacing:.16em;color:#92a9b5;font-size:12px;font-weight:bold}}
.level{{font-size:38px;font-weight:900;margin-top:16px}} .panel,.sector{{border:1px solid #29404c;background:#0c1b24;padding:18px;margin-top:12px}}
.sector h2{{display:flex;justify-content:space-between;gap:16px;font-size:18px}} .sector h2 span{{font-size:14px;color:#f6b46b}}
p,li{{line-height:1.55;color:#c6d2d8}} h2{{margin:0 0 10px}} ul{{padding-left:20px}}
footer{{color:#6f8793;font-size:12px;margin:25px 0}}
</style></head>
<body><main>
<header><div class="eyebrow">INFRAWATCH · POINT DE SITUATION NATIONAL</div>
<h1>{safe_text(document['cycle'])} · France</h1>
<div class="level">{safe_text(fmt_level(document['national'].get('level','ND')))}</div>
<p>Facteur dimensionnant : <strong>{safe_text(document['driver'].get('sector','ND'))}</strong></p></header>

<section class="panel"><div class="eyebrow">SYNTHÈSE NATIONALE</div><p>{safe_text(document['national_synthesis'])}</p></section>
{''.join(rows)}
<section class="panel"><div class="eyebrow">DYNAMIQUES INTERSECTORIELLES</div><ul>{dynamics or '<li>Aucune dynamique caractérisée.</li>'}</ul></section>
<section class="panel"><div class="eyebrow">POINTS DE SURVEILLANCE · PROCHAIN CYCLE</div><ul>{watches}</ul></section>
<section class="panel"><div class="eyebrow">APPRÉCIATION NATIONALE</div><p>{safe_text(document['national_assessment'])}</p></section>
<footer>Niveaux et métriques : moteur InfraWatch. Analyse qualitative : LLM. Aucune causalité automatique.</footer>
</main></body></html>"""
    output.write_text(html, encoding="utf-8")


class NumberedCanvasMixin:
    pass


def render_pdf(document: dict[str, Any], output: Path) -> None:
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TitleIW", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=17, leading=20, textColor=colors.HexColor("#15232c"),
        spaceAfter=4
    )
    eyebrow = ParagraphStyle(
        "EyebrowIW", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=7.2, leading=9, textColor=colors.HexColor("#607884"),
        spaceAfter=4
    )
    h2 = ParagraphStyle(
        "H2IW", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=10.5, leading=13, textColor=colors.HexColor("#13232c"),
        spaceBefore=5, spaceAfter=5
    )
    body = ParagraphStyle(
        "BodyIW", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=8.5, leading=12.1, textColor=colors.HexColor("#263b45"),
        spaceAfter=6
    )
    small = ParagraphStyle(
        "SmallIW", parent=body, fontSize=7.1, leading=9.5,
        textColor=colors.HexColor("#607884")
    )
    center = ParagraphStyle(
        "CenterIW", parent=body, alignment=TA_CENTER, fontName="Helvetica-Bold"
    )

    doc = BaseDocTemplate(
        str(output), pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=14*mm,
        title=f"InfraWatch - Point national {document['cycle']}",
        author="HCFRN / InfraWatch"
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(colors.HexColor("#718792"))
        canvas.drawString(15*mm, 8*mm, "INFRAWATCH - Niveaux et métriques issus du moteur. Analyse LLM qualitative uniquement.")
        canvas.drawRightString(A4[0]-15*mm, 8*mm, f"Page {_doc.page}")
        canvas.restoreState()

    doc.addPageTemplates(PageTemplate(id="main", frames=frame, onPage=footer))

    story = []
    story.append(Paragraph("INFRAWATCH · POINT DE SITUATION NATIONAL", eyebrow))
    story.append(Paragraph(f"{document['cycle']} · France", title))

    national_level = document["national"].get("level", "ND")
    national_table = Table([
        [
            Paragraph("<b>ÉTAT NATIONAL</b>", eyebrow),
            Paragraph("<b>FACTEUR DIMENSIONNANT</b>", eyebrow),
        ],
        [
            Paragraph(f"<b>{safe_text(fmt_level(national_level))}</b>", ParagraphStyle("Big", parent=body, fontName="Helvetica-Bold", fontSize=18, leading=21)),
            Paragraph(f"<b>{safe_text(document['driver'].get('sector','ND'))}</b><br/>{safe_text(document['driver'].get('metric',''))}", body),
        ],
    ], colWidths=[doc.width*0.48, doc.width*0.52])
    national_table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#eef3f5")),
        ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#a9bac2")),
        ("INNERGRID",(0,0),(-1,-1),0.35,colors.HexColor("#c5d1d6")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),7),
        ("RIGHTPADDING",(0,0),(-1,-1),7),
        ("TOPPADDING",(0,0),(-1,-1),6),
        ("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story += [national_table, Spacer(1, 5*mm)]

    story.append(Paragraph("SYNTHÈSE NATIONALE", eyebrow))
    story.append(Paragraph(safe_text(document["national_synthesis"]), body))
    story.append(Spacer(1, 2*mm))

    for sid in SECTOR_ORDER:
        s = document["sectors"][sid]
        block = [
            Paragraph(f"{safe_text(s['label']).upper()} · {safe_text(fmt_level(s['official_level']))}", h2),
            Paragraph(safe_text(s["analysis"]), body),
        ]
        story.append(KeepTogether(block))

    story.append(Spacer(1, 2*mm))
    story.append(Paragraph("DYNAMIQUES INTERSECTORIELLES", eyebrow))
    if document["intersectoral_dynamics"]:
        for item in document["intersectoral_dynamics"]:
            story.append(Paragraph(
                f"<b>{safe_text(item['title'])}</b> [{safe_text(item['status'])}] — {safe_text(item['analysis'])}",
                body
            ))
    else:
        story.append(Paragraph("Aucune dynamique intersectorielle caractérisée dans les données du cycle.", body))

    story.append(Spacer(1, 2*mm))
    story.append(Paragraph("POINTS DE SURVEILLANCE · PROCHAIN CYCLE", eyebrow))
    for item in document["surveillance_points"]:
        story.append(Paragraph(
            f"<b>{safe_text(item['title'])}</b> — {safe_text(item['analysis'])}",
            body
        ))

    story.append(Spacer(1, 2*mm))
    story.append(Paragraph("APPRÉCIATION NATIONALE", eyebrow))
    story.append(Paragraph(safe_text(document["national_assessment"]), body))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "Doctrine : niveaux N0-N4, facteur dimensionnant, seuils et métriques issus exclusivement du moteur InfraWatch. "
        "Le LLM produit uniquement la couche qualitative et ne peut modifier le scoring ni établir une causalité automatique.",
        small
    ))

    doc.build(story)


def archive_name(generated_at: str | None, cycle: str) -> str:
    dt = parse_dt(generated_at) or datetime.now(timezone.utc)
    local = dt.astimezone(PARIS)
    cycle_slug = cycle.lower().replace("h", "h")
    return f"{local:%Y-%m-%d}_{cycle_slug}"


def publish_outputs(document: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)

    stem = archive_name(document.get("generated_at"), document["cycle"])
    json_path = archive / f"{stem}.json"
    html_path = archive / f"{stem}.html"
    pdf_path = archive / f"{stem}.pdf"

    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html(document, html_path)
    render_pdf(document, pdf_path)

    shutil.copy2(json_path, out_dir / "latest.json")
    shutil.copy2(html_path, out_dir / "latest.html")
    shutil.copy2(pdf_path, out_dir / "latest.pdf")

    manifest = {
        "schema_version": "point-national-manifest-1.0",
        "cycle": document["cycle"],
        "generated_at": document.get("generated_at"),
        "latest_json": "latest.json",
        "latest_html": "latest.html",
        "latest_pdf": "latest.pdf",
        "archive_json": f"archive/{json_path.name}",
        "archive_html": f"archive/{html_path.name}",
        "archive_pdf": f"archive/{pdf_path.name}",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", default=".")
    parser.add_argument("--public-base", default=os.environ.get("INFRAWATCH_PUBLIC_BASE", "https://hcfrnveille.github.io/Infrawatch-public"))
    parser.add_argument("--output-dir", default="points")
    parser.add_argument("--prompt", default="point_national_prompt_v1.md")
    parser.add_argument("--schema", default="point_national_analysis_schema_v1.json")
    parser.add_argument("--cycle", default=os.environ.get("POINT_NATIONAL_FORCE_CYCLE"))
    parser.add_argument("--model", default=os.environ.get("POINT_NATIONAL_MODEL", "gpt-5.6-terra"))
    parser.add_argument("--facts-only", action="store_true")
    args = parser.parse_args()

    now = datetime.now(PARIS)
    cycle = current_cycle(now, args.cycle)
    sources, origins = load_sources(Path(args.source_dir), args.public_base)
    validate_core_sources(sources)

    facts = build_fact_packet(sources, origins, cycle, now)
    facts_path = Path(args.output_dir) / "fact_packet_latest.json"
    facts_path.parent.mkdir(parents=True, exist_ok=True)
    facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.facts_only:
        print(f"Fact packet écrit : {facts_path}")
        return 0

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    analysis = call_llm(
        facts,
        load_prompt(Path(args.prompt)),
        schema,
        args.model,
    )
    validate_analysis(analysis)
    document = build_final_document(facts, analysis)
    publish_outputs(document, Path(args.output_dir))
    print(f"Point national généré : {Path(args.output_dir) / 'latest.pdf'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
