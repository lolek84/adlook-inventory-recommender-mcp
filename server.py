import os
import glob
import json
import re
from typing import Optional
import pandas as pd
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

_port = int(os.environ.get("PORT", 8000))

mcp = FastMCP(
    "adlook-inventory-recommender",
    host="0.0.0.0",
    port=_port,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

INVENTORY_DIR = os.environ.get("INVENTORY_DIR", ".")
INVENTORY_FILE = os.environ.get("INVENTORY_FILE", "inventory_db_20260512.csv")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

INDUSTRY_MAP = {
    "fitness":      {"iab": ["IAB7"], "content": ["health", "sports"], "audience": ["mass_reach", "young_adults"]},
    "sport":        {"iab": ["IAB17"], "content": ["sports"], "audience": ["mass_reach", "young_adults"]},
    "health":       {"iab": ["IAB7"], "content": ["health"], "audience": ["mass_reach", "professionals"]},
    "food":         {"iab": ["IAB8"], "content": ["health", "other"], "audience": ["mass_reach", "parents"]},
    "fmcg":         {"iab": ["IAB8"], "content": ["other"], "audience": ["mass_reach", "parents"]},
    "automotive":   {"iab": ["IAB2"], "content": ["automotive"], "audience": ["mass_reach", "professionals"]},
    "finance":      {"iab": ["IAB13"], "content": ["finance"], "audience": ["professionals", "high_income"]},
    "banking":      {"iab": ["IAB13"], "content": ["finance"], "audience": ["professionals", "high_income"]},
    "ecommerce":    {"iab": ["IAB22"], "content": ["shopping"], "audience": ["mass_reach", "young_adults"]},
    "retail":       {"iab": ["IAB22"], "content": ["shopping"], "audience": ["mass_reach", "young_adults"]},
    "tech":         {"iab": ["IAB19"], "content": ["technology", "gaming"], "audience": ["young_adults", "tech_enthusiasts"]},
    "gaming":       {"iab": ["IAB9"], "content": ["gaming"], "audience": ["young_adults", "mass_reach"]},
    "fashion":      {"iab": ["IAB18"], "content": ["lifestyle"], "audience": ["young_adults", "high_income"]},
    "beauty":       {"iab": ["IAB18"], "content": ["lifestyle"], "audience": ["young_adults", "high_income"]},
    "education":    {"iab": ["IAB5"], "content": ["education"], "audience": ["students", "professionals"]},
    "family":       {"iab": ["IAB6"], "content": ["parenting"], "audience": ["parents"]},
    "news":         {"iab": ["IAB12"], "content": ["news"], "audience": ["mass_reach", "professionals"]},
    "entertainment":{"iab": ["IAB1"], "content": ["entertainment"], "audience": ["mass_reach", "young_adults"]},
    "travel":       {"iab": ["IAB20"], "content": ["other"], "audience": ["mass_reach", "high_income"]},
}

ALLOWED_INDUSTRIES = frozenset(INDUSTRY_MAP.keys())

_AGENT_PROMPT_CACHE: Optional[str] = None

_LANG_MAP = {
    "PL": "pl", "BR": "pt", "ES": "es", "MX": "es", "AR": "es",
    "FR": "fr", "BE": "fr", "GB": "en", "US": "en", "ZA": "en", "AU": "en",
    "IT": "it", "DE": "de", "AT": "de", "RO": "ro", "HU": "hu", "SE": "sv",
    "NL": "nl", "PT": "pt", "CZ": "cs", "SK": "sk", "HR": "hr",
}


def _get_agent_prompt_text() -> str:
    global _AGENT_PROMPT_CACHE
    if _AGENT_PROMPT_CACHE is not None:
        return _AGENT_PROMPT_CACHE
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AGENT_PROMPT.md")
    override = os.environ.get("AGENT_PROMPT_PATH")
    if override:
        path = override
    try:
        with open(path, encoding="utf-8") as f:
            _AGENT_PROMPT_CACHE = f.read()
    except OSError:
        _AGENT_PROMPT_CACHE = ""
    return _AGENT_PROMPT_CACHE


_BRIEF_JSON_INSTRUCTIONS = """Zwróć WYŁĄCZNIE jeden obiekt JSON (bez markdown) z polami:
- industry: string lub null — jedna z listy: fitness, sport, health, food, fmcg, automotive, finance, banking, ecommerce, retail, tech, gaming, fashion, beauty, education, family, news, entertainment, travel; null jeśli nie da się sensownie przypisać
- campaign_goal: jedna z: awareness, consideration, performance
- budget_usd: number lub null
- countries: tablica kodów ISO 3166-1 alpha-2 WIELKIMI LITERAMI (np. PL, US) lub null
- preferred_formats: tablica z: VIDEO, DISPLAY, NATIVE lub null
- preferred_devices: tablica z: Mobile, Desktop, CTV lub null
- content_types: tablica stringów lub null
- audience_profiles: tablica (np. young_adults, mass_reach) lub null
- confidence: number od 0 do 1
- brief_summary_pl: jedno zdanie po polsku: o co chodzi w kampanii"""


def _parse_brief_with_llm(client_brief: str) -> tuple[dict, Optional[str]]:
    try:
        import anthropic
    except ImportError as e:
        return {}, f"anthropic package not installed: {e}"

    if not ANTHROPIC_API_KEY:
        return {}, "ANTHROPIC_API_KEY not set"

    agent_doc = _get_agent_prompt_text()
    system_content = (
        f"{agent_doc}\n\n---\n\n" if agent_doc.strip() else ""
    ) + (
        "Jesteś backendem narzędzia MCP: z briefu klienta wyłuskujesz pola strukturalne "
        "do filtrowania inventory. Odpowiadasz wyłącznie jednym poprawnym obiektem JSON, bez markdown."
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        user_msg = f"Brief klienta:\n\n{client_brief.strip()}\n\n{_BRIEF_JSON_INSTRUCTIONS}"
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=system_content,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, f"Invalid JSON from model: {e}"
    except Exception as e:
        return {}, str(e)
    return data, None


def _normalize_llm_industry(raw: Optional[str]) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    return key if key in ALLOWED_INDUSTRIES else None


def _normalize_campaign_goal(raw: Optional[str]) -> str:
    if not raw or not isinstance(raw, str):
        return "awareness"
    g = raw.strip().lower()
    return g if g in ("awareness", "consideration", "performance") else "awareness"


def _pct_str_to_float(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().rstrip("%")
    try:
        v = float(s)
        return v / 100 if v > 1.5 else v
    except (ValueError, TypeError):
        return 0.0


def _normalize_inventory_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names, types and derived columns for inventory/history DataFrames."""
    df = df.copy()
    df.columns = df.columns.str.strip().str.lstrip("﻿").str.lstrip("﻿")

    col_rename = {
        "ADVERTISER_NAME": "advertiser_name",
        "SUPPLY_SOURCE": "supply_source",
        "DOMAIN": "domain",
        "APP_NAME": "app_name",
        "DEVICE_TYPE": "device_type",
        "ENVIRONMENT": "environment",
        "LINE_ITEM_TYPE": "line_item_type",
        "CREATIVE_SIZE": "creative_size",
        "COUNTRY": "country_focus",
        "IMPRESSIONS": "impressions",
        "VIEWABLE_IMPRESSIONS": "viewable_impressions",
        "MEASURABLE_IMPRESSIONS": "measurable_impressions",
        "VIEWABILITY": "viewability",
        "CLICKS": "clicks",
        "CLICK_THROUGH_RATE": "ctr",
        "TOTAL_SPEND_USD": "total_spend_usd",
        "ECPM_USD": "ecpm_usd",
        "VCPM_USD": "vcpm_usd",
        "ECPC_USD": "ecpc_usd",
        "VIDEO_COMPLETE_VIEWS": "video_complete_views",
        "VIDEO_COMPLETION_RATE": "video_completion_rate",
    }
    df = df.rename(columns={k: v for k, v in col_rename.items() if k in df.columns})

    if "country_focus" not in df.columns and "country" in df.columns:
        df["country_focus"] = df["country"]

    for col in ("viewability", "ctr", "video_completion_rate"):
        if col in df.columns:
            df[col] = (
                pd.to_numeric(df[col].astype(str).str.rstrip("%"), errors="coerce")
                .apply(lambda v: v / 100 if pd.notna(v) and v > 1.5 else v)
                .fillna(0.0)
            )

    if "publisher_tier" not in df.columns and "quality_score" in df.columns:
        df["publisher_tier"] = df["quality_score"].apply(
            lambda x: "premium" if float(x or 0) >= 0.80
            else ("mid-tier" if float(x or 0) >= 0.60 else "long-tail")
        )

    if "geo_focus" not in df.columns:
        df["geo_focus"] = df.get("country_focus", pd.Series(dtype=str)).apply(
            lambda x: "global" if not x or str(x).upper() in ("GLOBAL", "NAN", "") else "national"
        )

    if "is_app" not in df.columns:
        df["is_app"] = df.get("app_name", pd.Series(dtype=str)).apply(
            lambda x: bool(x and str(x).strip() not in ("", "nan", "None"))
        )

    if "language" not in df.columns and "country_focus" in df.columns:
        df["language"] = df["country_focus"].apply(
            lambda x: _LANG_MAP.get(str(x).upper(), "en")
        )

    for col in ("brand_safety_risk", "publisher_tier", "line_item_type",
                "device_type", "environment", "content_type",
                "geo_focus", "country_focus", "iab_category", "iab_category_secondary"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lower()

    if "audience_profile" in df.columns:
        df["audience_profile"] = df["audience_profile"].astype(str).str.strip().str.lower()

    return df


def _load_latest_inventory() -> tuple[pd.DataFrame, str]:
    path = os.path.join(INVENTORY_DIR, INVENTORY_FILE)
    if not os.path.isfile(path):
        pattern = os.path.join(INVENTORY_DIR, "inventory_*.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(f"No inventory CSV files found in {INVENTORY_DIR}")
        raise FileNotFoundError(
            f"Configured inventory file not found: {INVENTORY_FILE}. "
            f"Available: {', '.join(os.path.basename(f) for f in files)}"
        )
    df = pd.read_csv(path)
    return _normalize_inventory_df(df), os.path.basename(path)


def _load_history_db() -> Optional[tuple[pd.DataFrame, str]]:
    """Load campaign history DB (per-advertiser placement data) if available."""
    candidates: list[str] = []
    if HISTORY_FILE:
        p = os.path.join(INVENTORY_DIR, HISTORY_FILE)
        if os.path.isfile(p):
            candidates = [p]
    if not candidates:
        pattern = os.path.join(INVENTORY_DIR, "campaign_history_db_*.csv")
        candidates = sorted(glob.glob(pattern))
    if not candidates:
        return None
    path = candidates[-1]
    df = pd.read_csv(path)
    return _normalize_inventory_df(df), os.path.basename(path)


def _compute_segment_benchmarks(df: pd.DataFrame) -> dict:
    """Return median performance metrics for a (pre-filtered) segment DataFrame."""
    if df.empty:
        return {"placements_in_segment": 0}

    def med(col: str) -> float:
        if col not in df.columns:
            return 0.0
        return round(float(df[col].astype(float).median()), 4)

    total = len(df)
    low_bs = int((df["brand_safety_risk"] == "low").sum()) if "brand_safety_risk" in df.columns else total

    return {
        "placements_in_segment": total,
        "pct_low_brand_safety": round(low_bs / total * 100, 1) if total else 0.0,
        "median_viewability": med("viewability"),
        "median_vcr": med("video_completion_rate"),
        "median_ctr": med("ctr"),
        "median_ecpm_usd": med("ecpm_usd"),
        "median_quality_score": med("quality_score"),
        "median_cost_efficiency_score": med("cost_efficiency_score"),
        "median_brand_safety_score": med("brand_safety_score"),
    }


def _vs_benchmark(val: float, median: float) -> Optional[float]:
    """Return % deviation of val from median. None if median is 0."""
    if not median:
        return None
    return round((val - median) / median * 100, 1)


_INSIGHTS_SYSTEM = (
    "Jesteś ekspertem media planning w Adlook. Analizujesz dane historyczne inventory "
    "i piszesz zwięzłe wnioski po polsku dla planera mediów."
)


def _generate_insights_narrative(
    segment_info: dict,
    benchmarks: dict,
    top_performers: list[dict],
    avoid_list: list[dict],
) -> str:
    """Call Claude to generate a Polish-language narrative summarising segment insights."""
    try:
        import anthropic
    except ImportError:
        return ""
    if not ANTHROPIC_API_KEY:
        return ""

    def fmt_pct(v: float) -> str:
        return f"{v * 100:.1f}%" if v <= 1.0 else f"{v:.1f}%"

    top_lines = "\n".join(
        f"- {p.get('domain') or p.get('app_name') or 'unknown'}: "
        f"viewability={fmt_pct(float(p.get('viewability') or 0))}, "
        f"VCR={fmt_pct(float(p.get('video_completion_rate') or 0))}, "
        f"quality={float(p.get('quality_score') or 0):.2f}"
        for p in top_performers[:5]
    ) or "brak danych"

    avoid_lines = "\n".join(
        f"- {p.get('domain') or p.get('app_name') or 'unknown'}: "
        f"brand_safety={p.get('brand_safety_risk')}, "
        f"viewability={fmt_pct(float(p.get('viewability') or 0))}"
        for p in avoid_list[:3]
    ) or "brak ostrzeżeń"

    user_msg = (
        f"Segment: branża {segment_info.get('industry', 'nieznana')}, "
        f"kraje {segment_info.get('countries', 'wszystkie')}, "
        f"format {segment_info.get('formats', 'wszystkie')}.\n\n"
        f"Benchmarki segmentu ({benchmarks.get('placements_in_segment', 0)} placementów):\n"
        f"- Mediana viewability: {fmt_pct(benchmarks.get('median_viewability', 0))}\n"
        f"- Mediana VCR: {fmt_pct(benchmarks.get('median_vcr', 0))}\n"
        f"- Mediana CTR: {benchmarks.get('median_ctr', 0) * 100:.3f}%\n"
        f"- Mediana eCPM: ${benchmarks.get('median_ecpm_usd', 0):.2f}\n"
        f"- Mediana quality score: {benchmarks.get('median_quality_score', 0):.2f}\n"
        f"- % placementów bezpiecznych (brand safety = low risk): {benchmarks.get('pct_low_brand_safety', 0):.1f}%\n\n"
        f"Top placements:\n{top_lines}\n\n"
        f"Do unikania:\n{avoid_lines}\n\n"
        "Napisz 3–5 zdań po polsku: co sprawdza się w tym segmencie inventory i czego warto unikać. "
        "Odwołuj się do konkretnych domen i liczb tam gdzie możliwe."
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=350,
            system=_INSIGHTS_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return ""


def _detect_industry(query: str) -> Optional[str]:
    q = query.lower()
    for key in INDUSTRY_MAP:
        if key in q:
            return key
    return None


def _merge_brief_params(
    client_brief, budget_usd, countries, campaign_goal,
    preferred_formats, preferred_devices, parsed, llm_error,
):
    meta: dict = {
        "llm_used": bool(parsed is not None and not llm_error),
        "confidence": None,
        "brief_summary_pl": None,
        "llm_error": llm_error,
    }

    if llm_error or parsed is None:
        return (
            _detect_industry(client_brief),
            _normalize_campaign_goal(campaign_goal),
            budget_usd, countries, preferred_formats, preferred_devices,
            None, None, meta,
        )

    meta["confidence"] = float(parsed.get("confidence", 0) or 0)
    meta["brief_summary_pl"] = parsed.get("brief_summary_pl") if isinstance(parsed.get("brief_summary_pl"), str) else None

    ind = _normalize_llm_industry(parsed.get("industry")) or _detect_industry(client_brief)
    goal = _normalize_campaign_goal(campaign_goal or parsed.get("campaign_goal"))

    eff_budget = budget_usd if budget_usd is not None else parsed.get("budget_usd")
    if eff_budget is not None:
        try:
            eff_budget = float(eff_budget)
        except (TypeError, ValueError):
            eff_budget = budget_usd

    eff_countries = countries if countries is not None else parsed.get("countries")
    if eff_countries is not None and not isinstance(eff_countries, list):
        eff_countries = countries

    eff_formats = preferred_formats if preferred_formats is not None else parsed.get("preferred_formats")
    if eff_formats is not None:
        eff_formats = [str(x).upper() for x in eff_formats if x] if isinstance(eff_formats, list) else preferred_formats

    eff_devices = preferred_devices if preferred_devices is not None else parsed.get("preferred_devices")
    if eff_devices is not None and not isinstance(eff_devices, list):
        eff_devices = preferred_devices

    ct = parsed.get("content_types") if isinstance(parsed.get("content_types"), list) else None
    ap = parsed.get("audience_profiles") if isinstance(parsed.get("audience_profiles"), list) else None

    mapping = INDUSTRY_MAP.get((ind or "").lower(), {})
    if not ct and mapping.get("content"):
        ct = mapping["content"]
    if not ap and mapping.get("audience"):
        ap = mapping["audience"]

    return ind, goal, eff_budget, eff_countries, eff_formats, eff_devices, ct, ap, meta


def _audience_match(row_profile: str, requested: list[str]) -> bool:
    """Match comma-separated audience_profile against a list of requested profiles."""
    row_profiles = {p.strip() for p in row_profile.split(",")}
    return bool(row_profiles & set(requested))


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_inventory_files() -> dict:
    """List available inventory CSV files with their date ranges and row counts."""
    pattern = os.path.join(INVENTORY_DIR, "inventory_*.csv")
    files = sorted(glob.glob(pattern))
    result = []
    for f in files:
        name = os.path.basename(f)
        try:
            df = pd.read_csv(f)
            result.append({"file": name, "placements": len(df)})
        except Exception as e:
            result.append({"file": name, "error": str(e)})
    return {"inventory_files": result}


@mcp.tool()
def get_inventory_overview() -> dict:
    """
    Return a high-level overview of the inventory:
    formats, devices, countries, audience profiles, IAB categories,
    publisher tiers, brand safety distribution and aggregate stats.
    """
    df, filename = _load_latest_inventory()
    return {
        "source_file": filename,
        "total_placements": len(df),
        "total_impressions": int(df["impressions"].sum()),
        "total_spend_usd": round(float(df["total_spend_usd"].sum()), 2),
        "formats": df["line_item_type"].value_counts().to_dict(),
        "devices": df["device_type"].value_counts().to_dict(),
        "environments": df["environment"].value_counts().to_dict(),
        "publisher_tiers": df["publisher_tier"].value_counts().to_dict(),
        "audience_profiles": df["audience_profile"].value_counts().to_dict(),
        "content_types": df["content_type"].value_counts().to_dict(),
        "iab_categories": df["iab_category"].value_counts().to_dict(),
        "countries": df["country_focus"].value_counts().to_dict(),
        "brand_safety_risk": df["brand_safety_risk"].value_counts().to_dict(),
        "avg_viewability": round(float(df["viewability"].mean()), 4),
        "avg_quality_score": round(float(df["quality_score"].mean()), 4),
        "avg_ecpm_usd": round(float(df["ecpm_usd"].mean()), 4),
    }


@mcp.tool()
def find_placements(
    industry: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    audience_profiles: Optional[list[str]] = None,
    formats: Optional[list[str]] = None,
    devices: Optional[list[str]] = None,
    countries: Optional[list[str]] = None,
    min_viewability: float = 0.65,
    min_quality_score: float = 0.80,
    max_brand_safety_risk: str = "low",
    publisher_tiers: Optional[list[str]] = None,
    budget_usd: Optional[float] = None,
    limit: int = 50,
) -> dict:
    """
    Filter inventory placements by campaign criteria.

    Args:
        industry: Client industry keyword (e.g. 'fitness', 'automotive', 'finance').
        content_types: Filter by content type (e.g. ['health', 'sports']).
        audience_profiles: Filter by audience (e.g. ['young_adults', 'mass_reach']).
        formats: Ad formats (e.g. ['VIDEO', 'DISPLAY']).
        devices: Device types (e.g. ['Mobile', 'Desktop']).
        countries: Country codes (e.g. ['PL', 'US']). 'GLOBAL' always included.
        min_viewability: Minimum viewability rate (0–1). Default 0.65.
        min_quality_score: Minimum quality score (0–1). Default 0.80.
        max_brand_safety_risk: 'low' or 'medium'. Default 'low'.
        publisher_tiers: Allowed tiers (e.g. ['premium', 'mid-tier']).
        budget_usd: Max total spend in USD.
        limit: Max placements to return. Default 50.
    """
    df, filename = _load_latest_inventory()

    if industry:
        mapping = INDUSTRY_MAP.get(industry.lower(), {})
        if not content_types and mapping.get("content"):
            content_types = mapping["content"]
        if not audience_profiles and mapping.get("audience"):
            audience_profiles = mapping["audience"]

    allowed_risk = ["low", "medium"] if max_brand_safety_risk == "medium" else ["low"]
    df = df[df["brand_safety_risk"].isin(allowed_risk)]
    df = df[df["quality_score"].astype(float) >= min_quality_score]
    df = df[df["viewability"].astype(float) >= min_viewability]

    if content_types:
        ct_lower = [c.lower() for c in content_types]
        df = df[df["content_type"].isin(ct_lower)]

    if audience_profiles:
        ap_lower = [a.lower() for a in audience_profiles]
        df = df[df["audience_profile"].apply(lambda x: _audience_match(str(x), ap_lower))]

    if formats:
        fmt_upper = [f.upper() for f in formats]
        df = df[df["line_item_type"].str.upper().isin(fmt_upper)]

    if devices:
        dev_lower = [d.lower() for d in devices]
        df = df[df["device_type"].str.lower().isin(dev_lower)]

    if countries and len(df) > 0:
        country_upper = [c.upper() for c in countries] + ["GLOBAL"]
        df = df[df["country_focus"].apply(
            lambda x: any(c in str(x).upper() for c in country_upper)
        )]

    if publisher_tiers:
        pt_lower = [p.lower() for p in publisher_tiers]
        df = df[df["publisher_tier"].isin(pt_lower)]

    tier_order = {"premium": 0, "mid-tier": 1, "long-tail": 2}
    df["_tier_rank"] = df["publisher_tier"].map(tier_order).fillna(3)
    df["_score"] = df["quality_score"].astype(float) * df["cost_efficiency_score"].astype(float)
    df = df.sort_values(["_tier_rank", "_score"], ascending=[True, False])

    df = df.head(limit)

    cols = [
        "supply_source", "domain", "app_name", "environment", "device_type",
        "line_item_type", "creative_size", "impressions", "viewable_impressions",
        "clicks", "total_spend_usd", "viewability", "ctr", "ecpm_usd", "vcpm_usd",
        "ecpc_usd", "quality_score", "cost_efficiency_score", "brand_safety_score",
        "brand_safety_risk", "effective_viewability", "measurability_rate",
        "iab_category", "iab_category_secondary", "content_type", "audience_profile",
        "geo_focus", "country_focus", "language", "publisher_tier",
        "placement_rationale_pl",
    ]
    cols = [c for c in cols if c in df.columns]
    placements = df[cols].fillna("").to_dict(orient="records")

    summary = {
        "source_file": filename,
        "matched_placements": len(placements),
        "total_impressions": int(df["impressions"].astype(float).sum()),
        "total_viewable_impressions": int(df["viewable_impressions"].astype(float).sum()),
        "total_spend_usd": round(float(df["total_spend_usd"].astype(float).sum()), 2),
        "avg_viewability": round(float(df["viewability"].astype(float).mean()), 4) if len(df) else 0,
        "avg_ctr": round(float(df["ctr"].astype(float).mean()), 4) if len(df) else 0,
        "avg_ecpm_usd": round(float(df["ecpm_usd"].astype(float).mean()), 4) if len(df) else 0,
        "format_mix": df["line_item_type"].str.upper().value_counts().to_dict(),
        "device_mix": df["device_type"].str.lower().value_counts().to_dict(),
        "publisher_tier_mix": df["publisher_tier"].value_counts().to_dict(),
        "country_mix": df["country_focus"].value_counts().head(10).to_dict(),
    }

    return {"summary": summary, "placements": placements}


@mcp.tool()
def parse_client_brief(client_brief: str) -> dict:
    """
    Extract structured campaign parameters from a free-text client brief using Claude.
    Falls back to keyword heuristics if ANTHROPIC_API_KEY is not set.
    """
    ap_loaded = bool(_get_agent_prompt_text().strip())
    if not ANTHROPIC_API_KEY:
        return {
            "llm_available": False,
            "agent_prompt_loaded": ap_loaded,
            "heuristic_industry": _detect_industry(client_brief),
            "note": "Set ANTHROPIC_API_KEY to enable LLM-based parsing.",
        }
    data, err = _parse_brief_with_llm(client_brief)
    if err:
        return {
            "llm_available": True,
            "agent_prompt_loaded": ap_loaded,
            "error": err,
            "heuristic_industry": _detect_industry(client_brief),
        }
    return {
        "llm_available": True,
        "agent_prompt_loaded": ap_loaded,
        "parsed": data,
        "normalized_industry": _normalize_llm_industry(data.get("industry")) or _detect_industry(client_brief),
    }


@mcp.tool()
def get_campaign_insights(
    client_brief: str = "",
    industry: Optional[str] = None,
    countries: Optional[list[str]] = None,
    campaign_goal: Optional[str] = None,
    formats: Optional[list[str]] = None,
    top_n: int = 10,
) -> dict:
    """
    Return data-driven insights for a campaign type based on historical inventory performance:
    what placements work, what to avoid, segment benchmarks, and a Polish-language narrative.

    Optionally augmented with per-advertiser patterns if campaign_history_db_*.csv exists.

    Args:
        client_brief: Free-text campaign description (parsed by Claude when API key is set).
        industry: Industry keyword (e.g. 'fitness', 'automotive'). Overrides brief.
        countries: Target country codes (e.g. ['PL']). Overrides brief.
        campaign_goal: 'awareness' | 'consideration' | 'performance'. Overrides brief.
        formats: Ad formats to focus on (e.g. ['VIDEO', 'DISPLAY']). Overrides brief.
        top_n: Number of top performers and underperformers to return. Default 10.
    """
    # --- Parse brief if provided ---
    parsed: Optional[dict] = None
    llm_err: Optional[str] = None
    # Skip LLM brief parsing when all key params are explicitly provided
    _all_explicit = industry and countries and formats and campaign_goal
    if client_brief and ANTHROPIC_API_KEY and not _all_explicit:
        parsed, llm_err = _parse_brief_with_llm(client_brief)

    (
        eff_industry, eff_goal, _, eff_countries, eff_formats, _eff_devices,
        _content_types, _audience_profiles, brief_meta,
    ) = _merge_brief_params(
        client_brief, None, countries, campaign_goal,
        formats, None,
        parsed if not llm_err else None, llm_err,
    )

    # Explicit params override brief
    if industry:
        eff_industry = industry
    if countries:
        eff_countries = countries
    if formats:
        eff_formats = [f.upper() for f in formats]

    # --- Load inventory ---
    df, inv_filename = _load_latest_inventory()
    history_result = _load_history_db()

    # --- Determine IAB categories ---
    iab_cats: list[str] = []
    if eff_industry:
        iab_cats = INDUSTRY_MAP.get(eff_industry.lower(), {}).get("iab", [])

    # --- Build segment filter with progressive widening ---
    seg = df.copy()
    applied_filters: list[str] = []
    widening_notes: list[str] = []

    if iab_cats and "iab_category" in seg.columns:
        iab_lower = [i.lower() for i in iab_cats]
        # Exact prefix match: "iab2" must be followed by non-digit to avoid
        # "iab2" matching "iab20", "iab21", etc.
        def _iab_match(x: str) -> bool:
            xs = str(x).lower()
            return any(re.match(rf'^{re.escape(cat)}(\D|$)', xs) for cat in iab_lower)
        filtered = seg[seg["iab_category"].apply(_iab_match)]
        if len(filtered) >= 10:
            seg = filtered
            applied_filters.append(f"iab={','.join(iab_cats)}")
        else:
            widening_notes.append(
                f"IAB filter ({','.join(iab_cats)}) returned only {len(filtered)} placements "
                f"(threshold: 10) — widened to all categories."
            )

    if eff_countries:
        country_upper = [c.upper() for c in eff_countries] + ["GLOBAL"]
        filtered = seg[seg["country_focus"].apply(
            lambda x: any(c in str(x).upper() for c in country_upper)
        )]
        if len(filtered) >= 10:
            seg = filtered
            applied_filters.append(f"country={','.join(eff_countries)}")
        else:
            in_country = len(filtered)
            widening_notes.append(
                f"Country filter ({','.join(eff_countries)}) returned only {in_country} placements "
                f"(threshold: 10) — showing global {eff_industry or 'all'} inventory instead. "
                f"Consider refreshing inventory_db or broadening the country list."
            )

    if eff_formats:
        fmt_upper = [f.upper() for f in eff_formats]
        filtered = seg[seg["line_item_type"].str.upper().isin(fmt_upper)]
        if len(filtered) >= 5:
            seg = filtered
            applied_filters.append(f"format={','.join(fmt_upper)}")
        else:
            widening_notes.append(
                f"Format filter ({','.join(fmt_upper)}) returned only {len(filtered)} placements "
                f"(threshold: 5) — widened to all formats."
            )

    # --- Compute benchmarks ---
    benchmarks = _compute_segment_benchmarks(seg)

    # --- Score placements: quality × viewability × (1 + cost_efficiency), penalise brand risk ---
    # Vectorized — avoids slow row-by-row apply()
    _bsr_penalty = seg["brand_safety_risk"].map({"low": 0.0, "medium": 0.3, "high": 0.6}).fillna(0.0)
    seg = seg.copy()
    seg["_perf_score"] = (
        seg["quality_score"].astype(float).fillna(0.0)
        * seg["viewability"].astype(float).fillna(0.0)
        * (1 + seg["cost_efficiency_score"].astype(float).fillna(0.0))
        * (1 - _bsr_penalty)
    )
    # Deduplicate by domain+app_name: keep best-scoring row per placement identity
    seg["_place_key"] = seg["domain"].fillna("") + "|" + seg.get("app_name", pd.Series("", index=seg.index)).fillna("")
    seg_dedup = seg.sort_values("_perf_score", ascending=False).drop_duplicates(subset=["_place_key"])
    seg_sorted = seg_dedup.sort_values("_perf_score", ascending=False)

    top_df = seg_sorted.head(top_n)
    top_keys = set(top_df["_place_key"])

    avoid_mask = (
        seg_dedup["brand_safety_risk"].isin(["medium", "high"])
        if "brand_safety_risk" in seg_dedup.columns
        else pd.Series(False, index=seg_dedup.index)
    )
    if "quality_score" in seg_dedup.columns:
        q25 = seg_dedup["quality_score"].astype(float).quantile(0.25)
        avoid_mask = avoid_mask | (seg_dedup["quality_score"].astype(float) < q25)
    # Exclude domains already in top_performers to avoid contradictory signals
    avoid_df = seg_dedup[avoid_mask & ~seg_dedup["_place_key"].isin(top_keys)].sort_values("_perf_score", ascending=True).head(top_n)

    # --- Output columns ---
    output_cols = [
        "supply_source", "domain", "app_name", "environment", "device_type",
        "line_item_type", "creative_size", "impressions", "viewability",
        "video_completion_rate", "ctr", "ecpm_usd", "quality_score",
        "cost_efficiency_score", "brand_safety_score", "brand_safety_risk",
        "iab_category", "content_type", "audience_profile", "country_focus",
        "publisher_tier", "placement_rationale_pl",
    ]
    output_cols = [c for c in output_cols if c in seg.columns]

    med_viewability = benchmarks.get("median_viewability", 0)
    med_quality = benchmarks.get("median_quality_score", 0)

    def _enrich(sub_df: pd.DataFrame) -> list[dict]:
        records = sub_df[output_cols].fillna("").to_dict(orient="records")
        for r in records:
            warnings = []
            vb = float(r.get("viewability") or 0)
            qs = float(r.get("quality_score") or 0)
            if med_viewability > 0 and vb < med_viewability * 0.85:
                warnings.append(f"viewability {vb:.1%} is >15% below segment median {med_viewability:.1%}")
            if qs < 0.70:
                warnings.append(f"quality_score {qs:.3f} below recommended threshold 0.70")
            if warnings:
                r["warnings"] = warnings
            r["vs_benchmark"] = {
                "viewability_pct": _vs_benchmark(
                    float(r.get("viewability") or 0), benchmarks.get("median_viewability", 0)),
                "vcr_pct": _vs_benchmark(
                    float(r.get("video_completion_rate") or 0), benchmarks.get("median_vcr", 0)),
                "quality_score_pct": _vs_benchmark(
                    float(r.get("quality_score") or 0), benchmarks.get("median_quality_score", 0)),
                "ecpm_usd_pct": _vs_benchmark(
                    float(r.get("ecpm_usd") or 0), benchmarks.get("median_ecpm_usd", 0)),
            }
        return records

    top_performers = _enrich(top_df)
    avoid_list = _enrich(avoid_df)

    # --- History DB: per-advertiser patterns (Faza 2) ---
    history_summary: Optional[dict] = None
    if history_result is not None:
        hist_df, hist_filename = history_result
        hist_seg = hist_df.copy()

        if iab_cats and "iab_category" in hist_seg.columns:
            iab_lower = [i.lower() for i in iab_cats]
            filtered_h = hist_seg[hist_seg["iab_category"].apply(
                lambda x: any(cat in str(x).lower() for cat in iab_lower)
            )]
            if len(filtered_h) >= 5:
                hist_seg = filtered_h

        if eff_countries and "country_focus" in hist_seg.columns:
            country_upper = [c.upper() for c in eff_countries] + ["GLOBAL"]
            filtered_h = hist_seg[hist_seg["country_focus"].apply(
                lambda x: any(c in str(x).upper() for c in country_upper)
            )]
            if len(filtered_h) >= 5:
                hist_seg = filtered_h

        advertiser_count = 0
        top_advertisers: dict = {}
        if "advertiser_name" in hist_seg.columns and not hist_seg.empty:
            advertiser_count = int(hist_seg["advertiser_name"].nunique())
            top_advertisers = {
                str(k): int(v)
                for k, v in hist_seg.groupby("advertiser_name")["impressions"]
                .sum().nlargest(5).items()
            }

        # Top-performing placements per advertiser
        adv_top: list[dict] = []
        if "advertiser_name" in hist_seg.columns and not hist_seg.empty:
            hist_seg = hist_seg.copy()
            _h_bsr_penalty = hist_seg["brand_safety_risk"].map({"low": 0.0, "medium": 0.3, "high": 0.6}).fillna(0.0)
            hist_seg["_perf_score"] = (
                hist_seg["quality_score"].astype(float).fillna(0.0)
                * hist_seg["viewability"].astype(float).fillna(0.0)
                * (1 + hist_seg["cost_efficiency_score"].astype(float).fillna(0.0))
                * (1 - _h_bsr_penalty)
            )
            for adv, grp in hist_seg.groupby("advertiser_name"):
                hist_out = [c for c in output_cols if c in grp.columns]
                best = grp.nlargest(3, "_perf_score")[hist_out].fillna("").to_dict(orient="records")
                adv_top.append({
                    "advertiser": adv,
                    "impressions": int(grp["impressions"].sum()),
                    "avg_viewability": round(float(grp["viewability"].mean()), 4),
                    "avg_quality_score": round(float(grp["quality_score"].mean()), 4),
                    "top_placements": best,
                })
            adv_top.sort(key=lambda x: x["impressions"], reverse=True)

        history_summary = {
            "history_file": hist_filename,
            "placements_in_segment": len(hist_seg),
            "advertisers_in_segment": advertiser_count,
            "top_advertisers_by_impressions": top_advertisers,
            "benchmarks": _compute_segment_benchmarks(hist_seg),
            "advertiser_details": adv_top[:10],
        }

    # --- LLM narrative ---
    segment_info = {
        "industry": eff_industry or "nieznana",
        "countries": ",".join(eff_countries) if eff_countries else "wszystkie",
        "formats": ",".join(eff_formats) if eff_formats else "wszystkie",
    }
    narrative = _generate_insights_narrative(segment_info, benchmarks, top_performers, avoid_list)

    return {
        "segment": {
            "industry": eff_industry,
            "iab_categories": iab_cats,
            "countries": eff_countries,
            "formats": eff_formats,
            "campaign_goal": eff_goal,
            "applied_filters": applied_filters,
            "widening_notes": widening_notes,
            "source_file": inv_filename,
            "placements_analysed": len(seg),
        },
        "benchmark": benchmarks,
        "top_performers": top_performers,
        "avoid_list": avoid_list,
        "insights_pl": narrative,
        "brief_parsing": {
            "llm_used": brief_meta.get("llm_used"),
            "confidence": brief_meta.get("confidence"),
            "brief_summary_pl": brief_meta.get("brief_summary_pl"),
            "llm_error": brief_meta.get("llm_error"),
        },
        "history": history_summary,
    }


@mcp.tool()
def create_media_plan(
    client_brief: str,
    budget_usd: Optional[float] = None,
    countries: Optional[list[str]] = None,
    campaign_goal: Optional[str] = None,
    preferred_formats: Optional[list[str]] = None,
    preferred_devices: Optional[list[str]] = None,
    min_viewability: float = 0.70,
) -> dict:
    """
    Generate a complete media plan from a natural-language client brief.

    When ANTHROPIC_API_KEY is set, the brief is parsed by Claude to infer industry,
    goal, budget, countries, formats and devices. Explicit arguments override model output.

    Args:
        client_brief: Free-text description of the client and campaign.
        budget_usd: Total campaign budget in USD. Optional; overrides LLM if set.
        countries: Target country codes (e.g. ['PL']). Optional; overrides LLM if set.
        campaign_goal: 'awareness' | 'consideration' | 'performance'.
        preferred_formats: Override format selection (e.g. ['VIDEO']).
        preferred_devices: Override device selection (e.g. ['Mobile']).
        min_viewability: Minimum viewability threshold. Default 0.70.

    Returns:
        Structured media plan with selected placements, KPI projections,
        IAB categorization, placement rationales and strategic recommendations.
    """
    parsed: Optional[dict] = None
    llm_err: Optional[str] = None
    if ANTHROPIC_API_KEY:
        parsed, llm_err = _parse_brief_with_llm(client_brief)

    (
        industry, campaign_goal_eff, budget_eff, countries_eff,
        eff_formats, eff_devices, content_types, audience_profiles, brief_meta,
    ) = _merge_brief_params(
        client_brief, budget_usd, countries, campaign_goal,
        preferred_formats, preferred_devices,
        parsed if not llm_err else None, llm_err,
    )

    brief_lower = client_brief.lower()

    if eff_formats:
        formats = [f.upper() for f in eff_formats]
    elif campaign_goal_eff == "awareness":
        formats = ["VIDEO"]
    elif campaign_goal_eff == "consideration":
        formats = ["VIDEO", "DISPLAY"]
    else:
        formats = ["DISPLAY", "NATIVE"]

    if eff_devices:
        devices = eff_devices
    elif "desktop" in brief_lower:
        devices = ["Desktop"]
    elif "ctv" in brief_lower or "tv" in brief_lower:
        devices = ["CTV", "Mobile"]
    else:
        devices = ["Mobile", "Desktop"]

    # Scale placement limits proportionally to budget
    if not budget_eff:
        prem_limit, mid_limit = 20, 30
    elif budget_eff < 3_000:
        prem_limit, mid_limit = 10, 10
    elif budget_eff < 10_000:
        prem_limit, mid_limit = 20, 20
    elif budget_eff < 30_000:
        prem_limit, mid_limit = 30, 30
    else:
        prem_limit, mid_limit = 40, 40

    premium_result = find_placements(
        industry=industry,
        content_types=content_types,
        audience_profiles=audience_profiles,
        formats=formats,
        devices=devices,
        countries=countries_eff,
        min_viewability=min_viewability,
        min_quality_score=0.85,
        max_brand_safety_risk="low",
        publisher_tiers=["premium"],
        limit=prem_limit,
    )

    # Fallback: if no placements found with selected formats, widen to all formats
    if premium_result["summary"]["matched_placements"] == 0 and formats != ["DISPLAY", "NATIVE", "VIDEO"]:
        formats = ["DISPLAY", "VIDEO", "NATIVE"]
        premium_result = find_placements(
            industry=industry,
            content_types=content_types,
            audience_profiles=audience_profiles,
            formats=formats,
            devices=devices,
            countries=countries_eff,
            min_viewability=min_viewability,
            min_quality_score=0.85,
            max_brand_safety_risk="low",
            publisher_tiers=["premium"],
            limit=prem_limit,
        )

    midtier_result = find_placements(
        industry=industry,
        content_types=content_types,
        audience_profiles=audience_profiles,
        formats=formats,
        devices=devices,
        countries=countries_eff,
        min_viewability=min_viewability - 0.05,
        min_quality_score=0.80,
        max_brand_safety_risk="low",
        publisher_tiers=["mid-tier"],
        limit=mid_limit,
    )

    all_placements = premium_result["placements"] + midtier_result["placements"]
    seen: set = set()
    unique_placements = []
    for p in all_placements:
        key = (p.get("domain"), p.get("app_name"), p.get("line_item_type"), p.get("creative_size"))
        if key not in seen:
            seen.add(key)
            unique_placements.append(p)

    inv_impressions = sum(float(p.get("impressions", 0)) for p in unique_placements)
    inv_viewable = sum(float(p.get("viewable_impressions", 0)) for p in unique_placements)
    inv_clicks = sum(float(p.get("clicks", 0)) for p in unique_placements)

    avg_viewability = (
        sum(float(p.get("viewability", 0)) for p in unique_placements) / len(unique_placements)
        if unique_placements else 0
    )
    avg_ctr = (
        sum(float(p.get("ctr", 0)) for p in unique_placements) / len(unique_placements)
        if unique_placements else 0
    )

    if budget_eff and inv_impressions > 0:
        for p in unique_placements:
            share = float(p.get("impressions", 0)) / inv_impressions
            alloc = round(budget_eff * share, 2)
            ecpm = float(p.get("ecpm_usd") or 0)
            p["allocated_budget_usd"] = alloc
            p["projected_impressions"] = int(alloc / ecpm * 1000) if ecpm > 0 else 0
        proj_impressions = sum(p["projected_impressions"] for p in unique_placements)
        proj_viewable = int(proj_impressions * avg_viewability)
        proj_clicks = int(proj_impressions * avg_ctr)
        proj_spend = budget_eff
        budget_utilization = 100.0
    else:
        proj_impressions = int(inv_impressions)
        proj_viewable = int(inv_viewable)
        proj_clicks = int(inv_clicks)
        proj_spend = sum(float(p.get("total_spend_usd", 0)) for p in unique_placements)
        budget_utilization = None

    formats_used: dict = {}
    devices_used: dict = {}
    proj_imp_key = "projected_impressions" if budget_eff else "impressions"
    for p in unique_placements:
        f = p.get("line_item_type", "UNKNOWN").upper()
        d = p.get("device_type", "unknown").lower()
        imp = float(p.get(proj_imp_key, p.get("impressions", 0)))
        formats_used[f] = formats_used.get(f, 0) + imp
        devices_used[d] = devices_used.get(d, 0) + imp

    def pct_mix(counts: dict) -> dict:
        total = sum(counts.values()) or 1
        return {k: round(v / total * 100, 1) for k, v in counts.items()}

    recommendations = []
    if avg_viewability < 0.75:
        recommendations.append("Rozważ podniesienie progu viewability do 0.75 – obecna średnia jest blisko limitu.")
    if "VIDEO" not in formats_used and campaign_goal_eff == "awareness":
        recommendations.append("Dla kampanii awareness wideo jest kluczowe – rozszerz formaty o VIDEO.")
    if avg_ctr < 0.003:
        recommendations.append("Niski CTR – rozważ optymalizację kreacji lub przejście na targeting performance.")
    if len(unique_placements) < 5:
        recommendations.append("Mała liczba placementów – rozszerz kryteria (więcej kategorii lub mniejszy min. quality score).")
    if not recommendations:
        recommendations.append("Plan spełnia standardy jakościowe – monitoruj viewability i CTR w trakcie kampanii.")

    _, filename = _load_latest_inventory()

    return {
        "media_plan": {
            "source_file": filename,
            "client_brief": client_brief,
            "brief_parsing": {
                "llm_used": brief_meta.get("llm_used"),
                "claude_model": CLAUDE_MODEL if ANTHROPIC_API_KEY else None,
                "agent_prompt_loaded": bool(_get_agent_prompt_text().strip()),
                "confidence": brief_meta.get("confidence"),
                "brief_summary_pl": brief_meta.get("brief_summary_pl"),
                "llm_error": brief_meta.get("llm_error"),
            },
            "detected_industry": industry,
            "campaign_goal": campaign_goal_eff,
            "formats_requested": formats,
            "devices_requested": devices,
            "countries": countries_eff or ["GLOBAL"],
            "placements_count": len(unique_placements),
            "placements": unique_placements,
            "kpis": {
                "projected_impressions": proj_impressions,
                "projected_viewable_impressions": proj_viewable,
                "projected_clicks": proj_clicks,
                "projected_spend_usd": round(proj_spend, 2),
                "budget_usd": budget_eff,
                "budget_utilization_pct": budget_utilization,
                "avg_viewability_pct": round(avg_viewability * 100, 1),
                "avg_ctr_pct": round(avg_ctr * 100, 3),
                "avg_ecpm_usd": round(
                    sum(float(p.get("ecpm_usd", 0)) for p in unique_placements) / len(unique_placements), 4
                ) if unique_placements else 0,
                "inventory_placements": len(unique_placements),
            },
            "format_mix_pct": pct_mix(formats_used),
            "device_mix_pct": pct_mix(devices_used),
            "recommendations": recommendations,
        }
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
