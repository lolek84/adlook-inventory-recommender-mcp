import os
import glob
import json
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
        return v / 100 if v > 1.5 else v  # already a ratio if <= 1.5
    except (ValueError, TypeError):
        return 0.0


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
    df.columns = df.columns.str.strip().str.lstrip("﻿")

    # Normalize uppercase API column names → lowercase server names
    col_rename = {
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

    # Legacy: country / country_focus
    if "country_focus" not in df.columns and "country" in df.columns:
        df["country_focus"] = df["country"]

    # Convert percentage strings → float ratios (works regardless of dtype)
    for col in ("viewability", "ctr", "video_completion_rate"):
        if col in df.columns:
            df[col] = (
                pd.to_numeric(df[col].astype(str).str.rstrip("%"), errors="coerce")
                .apply(lambda v: v / 100 if pd.notna(v) and v > 1.5 else v)
                .fillna(0.0)
            )

    # Derive publisher_tier from quality_score if missing
    if "publisher_tier" not in df.columns and "quality_score" in df.columns:
        df["publisher_tier"] = df["quality_score"].apply(
            lambda x: "premium" if float(x or 0) >= 0.80
            else ("mid-tier" if float(x or 0) >= 0.60 else "long-tail")
        )

    # Derive geo_focus
    if "geo_focus" not in df.columns:
        df["geo_focus"] = df.get("country_focus", pd.Series(dtype=str)).apply(
            lambda x: "global" if not x or str(x).upper() in ("GLOBAL", "NAN", "") else "national"
        )

    # Derive is_app
    if "is_app" not in df.columns:
        df["is_app"] = df.get("app_name", pd.Series(dtype=str)).apply(
            lambda x: bool(x and str(x).strip() not in ("", "nan", "None"))
        )

    # Derive language from country
    if "language" not in df.columns and "country_focus" in df.columns:
        df["language"] = df["country_focus"].apply(
            lambda x: _LANG_MAP.get(str(x).upper(), "en")
        )

    # Normalize string columns to lowercase
    for col in ("brand_safety_risk", "publisher_tier", "line_item_type",
                "device_type", "environment", "content_type",
                "geo_focus", "country_focus", "iab_category", "iab_category_secondary"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lower()

    if "audience_profile" in df.columns:
        df["audience_profile"] = df["audience_profile"].astype(str).str.strip().str.lower()

    return df, os.path.basename(path)


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

    # Inventory totals (historical baseline)
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

    # Budget allocation: distribute budget proportionally by impression share,
    # then project impressions at that allocated spend using each placement's eCPM.
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
