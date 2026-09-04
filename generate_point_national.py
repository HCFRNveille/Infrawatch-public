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
    PageBreak, KeepTogether, Image
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

        four_operator_clusters = t.get("four_operator_clusters")
        if four_operator_clusters is None:
            clusters = t.get("multi_operator_coordinate_clusters") or []
            if isinstance(clusters, list):
                four_operator_clusters = sum(
                    1
                    for item in clusters
                    if isinstance(item, dict) and item.get("operators") == 4
                )

        return {
            "national_down_ratio_pct": t.get("national_down_ratio_pct"),
            "unique_operator_sites_down": t.get("unique_operator_sites_down"),
            "four_operator_clusters": four_operator_clusters,
            "top_departments": (
                t.get("top_departments")
                or t.get("top_operator_departments_by_ratio")
                or []
            ),
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



def build_source_health_context(
    dashboard: dict[str, Any],
    generated_at: Any,
    now: datetime,
) -> dict[str, Any]:
    """
    Sépare explicitement :
    - l'âge du snapshot dashboard utilisé par le générateur ;
    - la fraîcheur agrégée des sources telle que calculée par le backend.

    Ces deux valeurs n'ont pas la même sémantique et ne doivent jamais
    être comparées comme si elles mesuraient la même chose.
    """
    raw = copy.deepcopy(
        dashboard.get("source_health")
        or {}
    )

    backend_freshness = raw.pop(
        "freshness_minutes",
        None,
    )

    return {
        "dashboard_snapshot": {
            "generated_at": generated_at,
            "age_minutes_at_generation": freshness_minutes(
                generated_at,
                now,
            ),
            "meaning": (
                "Âge du dashboard.json utilisé pour construire ce point. "
                "Ce n'est pas une mesure de fraîcheur individuelle des sources."
            ),
        },
        "backend_source_health": {
            **raw,
            "reported_freshness_minutes": backend_freshness,
            "meaning": (
                "Fraîcheur agrégée déclarée par le backend InfraWatch pour "
                "les sources exploitées. Cette valeur est distincte de l'âge "
                "du snapshot dashboard."
            ),
        },
    }


def build_risks_threats_context(
    dashboard: dict[str, Any],
    sources: dict[str, Any],
) -> dict[str, Any]:
    """
    Normalise le bloc Risques & Menaces sans permettre au LLM
    d'inventer la nature de signaux non détaillés.

    Priorité :
    1. événements détaillés du fichier risks_threats.json s'ils existent ;
    2. sinon événements déjà exposés dans dashboard.json ;
    3. sinon compteurs uniquement avec garde-fou explicite.
    """
    dashboard_threats = (
        dashboard.get("threats")
        or {}
    )
    raw_threats = (
        sources.get("risks_threats.json")
        or {}
    )

    dashboard_events = dashboard_threats.get(
        "events"
    )
    raw_events = raw_threats.get(
        "events"
    )

    if not isinstance(
        dashboard_events,
        list,
    ):
        dashboard_events = []

    if not isinstance(
        raw_events,
        list,
    ):
        raw_events = []

    events = (
        raw_events
        if raw_events
        else dashboard_events
    )

    def first_value(key: str):
        if dashboard_threats.get(key) is not None:
            return dashboard_threats.get(key)
        return raw_threats.get(key)

    return {
        "collected": first_value("collected"),
        "recent": first_value("recent"),
        "relevant": first_value("relevant"),
        "impacts": first_value("impacts"),
        "events_detail_available": bool(events),
        "events": events[:10],
        "interpretation_guard": (
            "Les compteurs collected/recent/relevant/impacts peuvent être "
            "mentionnés comme des volumes uniquement. Si events_detail_available "
            "est false, la nature des événements, leur secteur, leur territoire, "
            "leur cause et leur impact précis sont inconnus et ne doivent jamais "
            "être inventés ou déduits."
        ),
    }

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
        "source_origins": origins,
        "doctrine": {
            "scoring_authority": "InfraWatch backend only",
            "llm_role": "qualitative analysis only",
            "no_frontend_or_llm_scoring": True,
            "no_automatic_causality": True,
        },
        "national": dashboard.get("national") or {},
        "driver": dashboard.get("driver") or {},
        "source_health": build_source_health_context(
            dashboard,
            generated_at,
            now,
        ),
        "sectors": sectors,
        "territories": dashboard.get("territories") or [],
        "correlations": dashboard.get("correlations") or [],
        "risks_threats": build_risks_threats_context(
            dashboard,
            sources,
        ),
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



def compact_current_for_document(sid: str, current: dict[str, Any]) -> dict[str, Any]:
    """
    Conserve uniquement les métriques nécessaires à la restitution finale.
    Le fact packet reste détaillé pour l'analyse LLM ; latest.json reste compact
    pour le dashboard, l'HTML, le PDF et les archives.
    """
    if sid == "electricity":
        keys = (
            "consumption_mw",
            "forecast_j_mw",
            "forecast_j1_mw",
            "forecast_error_pct",
            "physical_exchanges_mw",
            "exchange_ratio_pct",
            "production_total_mw",
            "production_by_fuel_mw",
        )
        return {key: current.get(key) for key in keys}

    if sid == "nuclear":
        keys = (
            "fleet_max_capacity_mw",
            "fleet_available_capacity_mw",
            "fleet_unavailable_capacity_mw",
            "fleet_availability_pct",
            "planned_current_events",
            "unplanned_current_events",
            "chronic_current_events",
            "current_operational_unavailability_events",
        )
        return {key: current.get(key) for key in keys}

    if sid == "gas":
        return {
            "operational_status": current.get("operational_status"),
            "data_quality": current.get("data_quality"),
            "limits_count": current.get("limits_count"),
            "green_limits_count": current.get("green_limits_count"),
            "orange_limits_count": current.get("orange_limits_count"),
            "red_limits_count": current.get("red_limits_count"),
            "violet_limits_count": current.get("violet_limits_count"),
            "unknown_limits_count": current.get("unknown_limits_count"),
            "alerts": [
                {
                    "limit": item.get("limit"),
                    "vigilance": item.get("vigilance"),
                    "application_side": item.get("application_side"),
                    "maintenance": item.get("maintenance"),
                }
                for item in (current.get("alerts") or [])
                if isinstance(item, dict)
            ][:10],
            "consumption_reference": current.get("consumption_reference") or {},
        }

    if sid == "fuel":
        keys = (
            "eligible_stations",
            "stations_with_any_shortage",
            "national_shortage_ratio_pct",
            "departments_with_shortage",
        )
        compact = {key: current.get(key) for key in keys}
        compact["top_departments"] = [
            {
                "dept_code": item.get("dept_code"),
                "shortage_ratio_pct": item.get("shortage_ratio_pct"),
                "stations_with_any_shortage": item.get("stations_with_any_shortage"),
                "eligible_stations": item.get("eligible_stations"),
            }
            for item in (current.get("top_departments") or [])
            if isinstance(item, dict)
        ][:10]
        return compact

    if sid == "telecom":
        compact = {
            "national_down_ratio_pct": current.get("national_down_ratio_pct"),
            "unique_operator_sites_down": current.get("unique_operator_sites_down"),
            "four_operator_clusters": current.get("four_operator_clusters"),
        }
        compact["top_departments"] = [
            {
                "operator_code": item.get("operator_code"),
                "dept_code": item.get("dept_code"),
                "down_ratio_pct": item.get("down_ratio_pct"),
                "sites_down": item.get("sites_down"),
            }
            for item in (current.get("top_departments") or [])
            if isinstance(item, dict)
        ][:10]
        return compact

    if sid == "rail":
        alerts = current.get("service_alerts") or {}
        return {
            "trips_count": current.get("trips_count"),
            "canceled_trips": current.get("canceled_trips"),
            "canceled_ratio_pct": current.get("canceled_ratio_pct"),
            "trips_delay_ge_30min_ratio_pct": current.get("trips_delay_ge_30min_ratio_pct"),
            "trips_delay_ge_60min_ratio_pct": current.get("trips_delay_ge_60min_ratio_pct"),
            "service_alerts": {
                "active_alerts_count": alerts.get("active_alerts_count"),
                "operational_alerts_count": alerts.get("operational_alerts_count"),
                "informational_or_unqualified_alerts_count": alerts.get(
                    "informational_or_unqualified_alerts_count"
                ),
                "effects": alerts.get("effects") or {},
                "causes": alerts.get("causes") or {},
            },
        }

    return current


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
                "current": compact_current_for_document(
                    sid,
                    facts["sectors"][sid]["current"],
                ),
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


def _format_datetime_fr(value: Any) -> str:
    dt = parse_dt(value)
    if dt is None:
        return "ND"
    local = dt.astimezone(PARIS)
    return f"{local:%d/%m/%Y} · {local:%H}h{local:%M}"


def _fmt_int(value: Any) -> str:
    if value is None:
        return "ND"
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return str(value)


def _fmt_pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "ND"
    try:
        return f"{float(value):.{digits}f} %".replace(".", ",")
    except Exception:
        return str(value)


def _fmt_signed_delta(value: Any, digits: int = 2) -> str:
    if value is None:
        return "Comparaison ND"
    try:
        val = float(value)
    except Exception:
        return str(value)

    # ASCII arrows avoided for maximum PDF renderer compatibility.
    direction = "Baisse" if val < 0 else ("Hausse" if val > 0 else "Stable")
    return f"{direction} · {val:+.{digits}f} pt".replace(".", ",")


def _fmt_gw(value_mw: Any, digits: int = 2) -> str:
    if value_mw is None:
        return "ND"
    try:
        return f"{float(value_mw) / 1000:.{digits}f} GW".replace(".", ",")
    except Exception:
        return str(value_mw)


def _trend_label(value: Any) -> str:
    mapping = {
        "up": "Hausse",
        "down": "Baisse",
        "stable": "Stable",
        "unknown": "Indéterminée",
        None: "Indéterminée",
    }
    return mapping.get(value, str(value))


def _level_hex(level: str) -> str:
    mapping = {
        "N0": "#26b36c",
        "N1": "#e2b84f",
        "N2": "#f08a38",
        "N3": "#d94b67",
        "N4": "#8b4cc7",
        "ND": "#8aa0ab",
    }
    return mapping.get(level, "#8aa0ab")


def _sector_metric_line(sid: str, current: dict[str, Any]) -> str:
    if sid == "fuel":
        return (
            f"{_fmt_pct(current.get('national_shortage_ratio_pct'))} · "
            f"{_fmt_int(current.get('stations_with_any_shortage'))} / "
            f"{_fmt_int(current.get('eligible_stations'))} stations"
        )

    if sid == "gas":
        return (
            f"Statut {current.get('operational_status') or 'ND'} · "
            f"{_fmt_int(current.get('orange_limits_count'))} limite(s) orange"
        )

    if sid == "telecom":
        clusters = current.get("four_operator_clusters")
        cluster_text = (
            f" · {_fmt_int(clusters)} cluster(s) 4 opérateurs"
            if clusters is not None
            else ""
        )
        return (
            f"{_fmt_pct(current.get('national_down_ratio_pct'))} · "
            f"{_fmt_int(current.get('unique_operator_sites_down'))} sites indisponibles"
            f"{cluster_text}"
        )

    if sid == "rail":
        return (
            f"{_fmt_pct(current.get('canceled_ratio_pct'))} annulations · "
            f"{_fmt_pct(current.get('trips_delay_ge_30min_ratio_pct'))} retards ≥ 30 min"
        )

    if sid == "electricity":
        value = current.get("forecast_error_pct")
        absolute = abs(float(value)) if value is not None else None
        return (
            f"Écart absolu {_fmt_pct(absolute)} · "
            f"consommation {_fmt_gw(current.get('consumption_mw'))}"
        )

    if sid == "nuclear":
        return (
            f"Disponibilité {_fmt_pct(current.get('fleet_availability_pct'))} · "
            f"{_fmt_gw(current.get('fleet_available_capacity_mw'))} disponibles"
        )

    return ""


def _build_panel(
    width: float,
    title: str,
    flowables: list[Any],
    *,
    accent: str = "#2a4350",
    bg: str = "#ffffff",
) -> Table:
    panel_header = ParagraphStyle(
        "PanelHeaderIW",
        fontName="Helvetica-Bold",
        fontSize=7.1,
        leading=8.7,
        textColor=colors.HexColor("#607884"),
        spaceAfter=4,
    )

    content = [Paragraph(safe_text(title), panel_header)] + flowables
    table = Table([[content]], colWidths=[width])

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#c8d5da")),
        ("LINEBEFORE", (0, 0), (0, 0), 3.0, colors.HexColor(accent)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def _normalize_dynamic_status(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip().lower()

    if raw in {"confirmed", "confirmée", "confirmee"}:
        return "Confirmée", "#d94b67"

    if raw in {"watch", "surveillance", "à surveiller", "a surveiller"}:
        return "À surveiller", "#e2b84f"

    if raw in {"none", "aucune", "non caractérisée", "non caracterisee", ""}:
        return "Non caractérisée", "#8aa0ab"

    return str(value), "#8aa0ab"


def _two_column_cards(cards: list[Any], total_width: float) -> Table:
    rows = []
    items = list(cards)

    while items:
        first = items.pop(0)
        second = items.pop(0) if items else Spacer(1, 1)
        rows.append([first, second])

    if not rows:
        rows = [[Spacer(1, 1), Spacer(1, 1)]]

    table = Table(rows, colWidths=[total_width / 2, total_width / 2])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


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


def render_pdf(
    document: dict[str, Any],
    output: Path,
    assets_dir: Path | None = None,
) -> None:
    styles = getSampleStyleSheet()

    title = ParagraphStyle(
        "TitleIW",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=21,
        textColor=colors.HexColor("#15232c"),
        spaceAfter=2,
    )
    subtitle = ParagraphStyle(
        "SubtitleIW",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.2,
        leading=10,
        textColor=colors.HexColor("#607884"),
        spaceAfter=0,
    )
    eyebrow = ParagraphStyle(
        "EyebrowIW",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.2,
        leading=8.8,
        textColor=colors.HexColor("#607884"),
        spaceAfter=3,
    )
    h2 = ParagraphStyle(
        "H2IW",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=10.3,
        leading=12.5,
        textColor=colors.HexColor("#13232c"),
        spaceAfter=2,
    )
    body = ParagraphStyle(
        "BodyIW",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7.8,
        leading=10.6,
        textColor=colors.HexColor("#263b45"),
        spaceAfter=3,
    )
    body_small = ParagraphStyle(
        "BodySmallIW",
        parent=body,
        fontSize=7.0,
        leading=9.2,
        spaceAfter=2,
    )
    metric = ParagraphStyle(
        "MetricIW",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=8.1,
        leading=10,
        textColor=colors.HexColor("#263b45"),
        spaceAfter=2,
    )
    big = ParagraphStyle(
        "BigIW",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=17.5,
        leading=20,
        textColor=colors.HexColor("#13232c"),
        spaceAfter=1,
    )
    note = ParagraphStyle(
        "NoteIW",
        parent=body,
        fontSize=6.8,
        leading=8.6,
        textColor=colors.HexColor("#607884"),
        spaceAfter=0,
    )

    doc = BaseDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=13 * mm,
        rightMargin=13 * mm,
        topMargin=13 * mm,
        bottomMargin=12 * mm,
        title=f"InfraWatch - Point national {document['cycle']}",
        author="HCFRN / InfraWatch",
    )

    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="normal",
    )

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#d5e0e5"))
        canvas.line(
            doc.leftMargin,
            10 * mm,
            A4[0] - doc.rightMargin,
            10 * mm,
        )
        canvas.setFont("Helvetica", 6.2)
        canvas.setFillColor(colors.HexColor("#718792"))
        canvas.drawString(
            doc.leftMargin,
            7.1 * mm,
            "INFRAWATCH - Niveaux et métriques issus du moteur ; analyse LLM qualitative ; aucune causalité automatique.",
        )
        canvas.drawRightString(
            A4[0] - doc.rightMargin,
            7.1 * mm,
            f"Page {_doc.page}",
        )
        canvas.restoreState()

    doc.addPageTemplates(
        PageTemplate(
            id="main",
            frames=frame,
            onPage=footer,
        )
    )

    national = document.get("national") or {}
    driver = document.get("driver") or {}
    source_health = document.get("source_health") or {}
    backend_health = source_health.get("backend_source_health") or {}
    snapshot_health = source_health.get("dashboard_snapshot") or {}

    usable = backend_health.get("usable")
    total = backend_health.get("total")
    snapshot_age = snapshot_health.get("age_minutes_at_generation")

    story: list[Any] = []

    # ---------------------------------------------------------
    # PAGE 1 - Situation opérationnelle nationale
    # ---------------------------------------------------------
    logo = None
    if assets_dir is not None:
        for candidate in (
            "logo_hcfrn.jpg",
            "logo_hcfrn.jpeg",
            "logo_hcfrn.png",
            "logo-hcfrn.jpg",
            "logo-hcfrn.png",
        ):
            candidate_path = assets_dir / candidate
            if candidate_path.exists():
                try:
                    logo = Image(
                        str(candidate_path),
                        width=24 * mm,
                        height=16 * mm,
                    )
                except Exception:
                    logo = None
                break

    header_text = [
        Paragraph(
            "INFRAWATCH · POINT DE SITUATION NATIONAL",
            eyebrow,
        ),
        Paragraph(
            f"{safe_text(document['cycle'])} · France",
            title,
        ),
        Paragraph(
            safe_text(_format_datetime_fr(document.get("generated_at"))),
            subtitle,
        ),
    ]

    if logo is not None:
        identity = Table(
            [[logo, header_text]],
            colWidths=[27 * mm, doc.width * 0.53 - 27 * mm],
        )
    else:
        identity = Table(
            [[header_text]],
            colWidths=[doc.width * 0.53],
        )

    identity.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    quality_text = (
        f"{_fmt_int(usable)}/{_fmt_int(total)} sources exploitables"
        if usable is not None and total is not None
        else "Qualité sources : ND"
    )

    header_status = [
        Paragraph(
            f"<b>{safe_text(fmt_level(national.get('level', 'ND')))}</b>",
            h2,
        ),
        Paragraph(
            f"Facteur dimensionnant : <b>{safe_text(driver.get('sector', 'ND'))}</b>",
            body_small,
        ),
        Paragraph(
            f"{safe_text(quality_text)} · snapshot {safe_text(_fmt_int(snapshot_age))} min",
            note,
        ),
    ]

    header = Table(
        [[identity, header_status]],
        colWidths=[doc.width * 0.62, doc.width * 0.38],
    )
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef3f5")),
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#c7d4d9")),
        (
            "LINEBEFORE",
            (0, 0),
            (0, 0),
            3.2,
            colors.HexColor(_level_hex(national.get("level", "ND"))),
        ),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    story.extend([
        header,
        Spacer(1, 3.5 * mm),
    ])

    state_card = _build_panel(
        doc.width / 3 - 3,
        "ÉTAT NATIONAL",
        [
            Paragraph(
                safe_text(fmt_level(national.get("level", "ND"))),
                big,
            ),
        ],
        accent=_level_hex(national.get("level", "ND")),
        bg="#f9fbfc",
    )

    driver_card = _build_panel(
        doc.width / 3 - 3,
        "FACTEUR DIMENSIONNANT",
        [
            Paragraph(
                safe_text(driver.get("sector", "ND")),
                h2,
            ),
            Paragraph(
                safe_text(driver.get("metric") or "ND"),
                metric,
            ),
            Paragraph(
                safe_text(_fmt_signed_delta(driver.get("delta"))),
                body_small,
            ),
        ],
        accent="#f08a38",
        bg="#fffaf6",
    )

    trend_card = _build_panel(
        doc.width / 3 - 3,
        "TENDANCE GÉNÉRALE",
        [
            Paragraph(
                safe_text(national.get("trend") or "INDÉTERMINÉE"),
                h2,
            ),
            Paragraph(
                safe_text(national.get("trend_detail") or "Lecture backend"),
                body_small,
            ),
        ],
        accent="#5aa5e8",
        bg="#f7fbfd",
    )

    summary_cards = Table(
        [[state_card, driver_card, trend_card]],
        colWidths=[
            doc.width / 3,
            doc.width / 3,
            doc.width / 3,
        ],
    )
    summary_cards.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    story.extend([
        summary_cards,
        Spacer(1, 3.5 * mm),
    ])

    story.append(
        _build_panel(
            doc.width,
            "SYNTHÈSE NATIONALE",
            [
                Paragraph(
                    safe_text(document.get("national_synthesis") or ""),
                    body,
                )
            ],
            accent=_level_hex(national.get("level", "ND")),
            bg="#ffffff",
        )
    )
    story.append(Spacer(1, 3.5 * mm))

    sector_cards = []

    for sid in SECTOR_ORDER:
        sector = document["sectors"][sid]
        current = sector.get("current") or {}
        level = sector.get("official_level", "ND")

        sector_cards.append(
            _build_panel(
                doc.width / 2 - 3,
                sector["label"].upper(),
                [
                    Paragraph(
                        f"<b>{safe_text(fmt_level(level))}</b>",
                        h2,
                    ),
                    Paragraph(
                        safe_text(_sector_metric_line(sid, current)),
                        metric,
                    ),
                    Paragraph(
                        f"Tendance backend : <b>{safe_text(_trend_label(sector.get('backend_trend')))}</b>",
                        note,
                    ),
                ],
                accent=_level_hex(level),
                bg="#ffffff",
            )
        )

    sector_matrix = Table(
        [
            [sector_cards[0], sector_cards[1]],
            [sector_cards[2], sector_cards[3]],
            [sector_cards[4], sector_cards[5]],
        ],
        colWidths=[doc.width / 2, doc.width / 2],
    )
    sector_matrix.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    story.append(sector_matrix)
    story.append(Spacer(1, 3.5 * mm))

    # Faits saillants : le facteur dimensionnant et le principal secteur
    # secondaire de vigilance, sans modifier le contenu analytique produit.
    highlight_ids = []
    driver_sid = driver.get("sector_id")
    if driver_sid in document.get("sectors", {}):
        highlight_ids.append(driver_sid)

    if "gas" in document.get("sectors", {}) and "gas" not in highlight_ids:
        highlight_ids.append("gas")

    if len(highlight_ids) < 2:
        ranked = sorted(
            document.get("sectors", {}).items(),
            key=lambda item: (
                {"N0": 0, "N1": 1, "N2": 2, "N3": 3, "N4": 4, "ND": -1}.get(
                    item[1].get("official_level", "ND"),
                    -1,
                )
            ),
            reverse=True,
        )
        for sid, _sector in ranked:
            if sid not in highlight_ids:
                highlight_ids.append(sid)
            if len(highlight_ids) == 2:
                break

    highlight_cards = []
    for sid in highlight_ids[:2]:
        sector = document["sectors"][sid]
        level = sector.get("official_level", "ND")
        highlight_cards.append(
            _build_panel(
                doc.width / 2 - 3,
                f"FAIT SAILLANT · {sector['label'].upper()}",
                [
                    Paragraph(
                        safe_text(sector.get("analysis") or ""),
                        body_small,
                    )
                ],
                accent=_level_hex(level),
                bg="#ffffff",
            )
        )

    if highlight_cards:
        if len(highlight_cards) == 1:
            highlights = Table(
                [[highlight_cards[0], Spacer(1, 1)]],
                colWidths=[doc.width / 2, doc.width / 2],
            )
        else:
            highlights = Table(
                [[highlight_cards[0], highlight_cards[1]]],
                colWidths=[doc.width / 2, doc.width / 2],
            )

        highlights.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(highlights)

    story.append(PageBreak())

    # ---------------------------------------------------------
    # PAGE 2 - Appréciation et surveillance
    # ---------------------------------------------------------
    story.append(
        Paragraph(
            "INFRAWATCH · APPRÉCIATION ET SURVEILLANCE",
            eyebrow,
        )
    )
    story.append(
        Paragraph(
            f"{safe_text(document['cycle'])} · Prochain cycle",
            title,
        )
    )
    story.append(Spacer(1, 2.5 * mm))

    dynamics = document.get("intersectoral_dynamics") or []

    if dynamics:
        dynamic_flowables = []
        for item in dynamics:
            status_label, status_color = _normalize_dynamic_status(
                item.get("status")
            )
            dynamic_flowables.extend([
                Paragraph(
                    f"<b>{safe_text(item.get('title', 'Dynamique intersectorielle'))}</b>",
                    h2,
                ),
                Paragraph(
                    f"Statut : <font color='{status_color}'><b>{safe_text(status_label)}</b></font>",
                    body_small,
                ),
                Paragraph(
                    safe_text(item.get("analysis") or ""),
                    body,
                ),
            ])
    else:
        dynamic_flowables = [
            Paragraph(
                "Aucune dynamique intersectorielle caractérisée dans les données du cycle.",
                body,
            )
        ]

    story.append(
        _build_panel(
            doc.width,
            "DYNAMIQUES INTERSECTORIELLES",
            dynamic_flowables,
            accent="#5aa5e8",
            bg="#f7fbfd",
        )
    )
    story.append(Spacer(1, 3.5 * mm))

    watch_cards = []
    for item in document.get("surveillance_points") or []:
        watch_cards.append(
            _build_panel(
                doc.width / 2 - 3,
                "POINT DE SURVEILLANCE",
                [
                    Paragraph(
                        f"<b>{safe_text(item.get('title', 'Point de surveillance'))}</b>",
                        body,
                    ),
                    Paragraph(
                        safe_text(item.get("analysis") or ""),
                        body_small,
                    ),
                ],
                accent="#f08a38",
                bg="#ffffff",
            )
        )

    story.append(
        _two_column_cards(
            watch_cards,
            doc.width,
        )
    )
    story.append(Spacer(1, 3.5 * mm))

    story.append(
        _build_panel(
            doc.width,
            "APPRÉCIATION NATIONALE",
            [
                Paragraph(
                    safe_text(document.get("national_assessment") or ""),
                    body,
                )
            ],
            accent=_level_hex(national.get("level", "ND")),
            bg="#f9fbfc",
        )
    )
    story.append(Spacer(1, 3 * mm))

    doctrine = (
        "Niveaux N0-N4, facteur dimensionnant, seuils et métriques issus "
        "exclusivement du moteur InfraWatch. Le LLM produit uniquement la "
        "couche qualitative et ne peut modifier le scoring. Les dynamiques "
        "intersectorielles ne valent pas causalité automatique."
    )

    story.append(
        _build_panel(
            doc.width,
            "CADRE DOCTRINAL",
            [
                Paragraph(
                    safe_text(doctrine),
                    note,
                )
            ],
            accent="#8aa0ab",
            bg="#ffffff",
        )
    )

    doc.build(story)


def archive_name(generated_at: str | None, cycle: str) -> str:
    dt = parse_dt(generated_at) or datetime.now(timezone.utc)
    local = dt.astimezone(PARIS)
    cycle_slug = cycle.lower().replace("h", "h")
    return f"{local:%Y-%m-%d}_{cycle_slug}"


def publish_outputs(document: dict[str, Any], out_dir: Path, assets_dir: Path | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)

    stem = archive_name(document.get("generated_at"), document["cycle"])
    json_path = archive / f"{stem}.json"
    html_path = archive / f"{stem}.html"
    pdf_path = archive / f"{stem}.pdf"

    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html(document, html_path)
    render_pdf(document, pdf_path, assets_dir=assets_dir)

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
    publish_outputs(document, Path(args.output_dir), assets_dir=Path(args.source_dir))
    print(f"Point national généré : {Path(args.output_dir) / 'latest.pdf'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
