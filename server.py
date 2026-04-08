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
INVENTORY_FILE = os.environ.get(
    "INVENTORY_FILE",
    "inventory_20251005_20260402_with_country.csv",
)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# IAB category mapping per industry
INDUSTRY_MAP = {
    "fitness":      {"iab": ["IAB7"], "content": ["health", "sports"], "audience": ["mass_reach", "young_adults"]},
    "sport":        {"iab": ["IAB7"], "content": ["health", "sports"], "audience": ["mass_reach", "young_adults"]},
    "health":       {"iab": ["IAB7"], "content": ["health"],           "audience": ["mass_reach", "professionals"]},
    "food":         {"iab": ["IAB8"], "content": ["health", "other"],  "audience": ["mass_reach", "parents"]},
    "fmcg":         {"iab": ["IAB8"], "content": ["other"],            "audience": ["mass_reach", "parents"]},
    "automotive":   {"iab": ["IAB2"], "content": ["automotive"],       "audience": ["mass_reach", "professionals"]},
    "finance":      {"iab": ["IAB13"], "content": ["finance"],         "audience": ["professionals", "high_income"]},
    "banking":      {"iab": ["IAB13"], "content": ["finance"],         "audience": ["professionals", "high_income"]},
    "ecommerce":    {"iab": ["IAB22"], "content": ["shopping"],        "audience": ["mass_reach", "young_adults"]},
    "retail":       {"iab": ["IAB22"], "content": ["shopping"],        "audience": ["mass_reach", "young_adults"]},
    "tech":         {"iab": ["IAB19", "IAB9"], "content": ["technology", "gaming"], "audience": ["young_adults", "tech_enthusiasts"]},
    "gaming":       {"iab": ["IAB9"], "content": ["gaming"],           "audience": ["young_adults", "mass_reach"]},
    "fashion":      {"iab": ["IAB18"], "content": ["entertainment"],   "audience": ["young_adults", "high_income"]},
    "beauty":       {"iab": ["IAB18"], "content": ["entertainment"],   "audience": ["young_adults", "high_income"]},
    "education":    {"iab": ["IAB5"], "content": ["education"],        "audience": ["students", "professionals"]},
    "family":       {"iab": ["IAB6"], "content": ["parenting"],        "audience": ["parents"]},
    "news":         {"iab": ["IAB12"], "content": ["news"],            "audience": ["mass_reach", "professionals"]},
    "entertainment":{"iab": ["IAB1"], "content": ["entertainment", "streaming"], "audience": ["mass_reach", "young_adults"]},
    "travel":       {"iab": ["IAB20"], "content": ["other"],           "audience": ["mass_reach", "high_income"]},
}

ALLOWED_INDUSTRIES = frozenset(INDUSTRY_MAP.keys())

_AGENT_PROMPT_CACHE: Optional[str] = None


def _get_agent_prompt_text() -> str:
    """Load AGENT_PROMPT.md once (same directory as server.py). Empty if missing."""
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
- budget_usd: number lub null (tylko jeśli w briefie jest kwota lub jednoznacznie można ją wywnioskować)
- countries: tablica kodów ISO 3166-1 alpha-2 WIELKIMI LITERAMI (np. PL, US) lub null
- preferred_formats: tablica z: VIDEO, DISPLAY, NATIVE lub null (null = wybierz wg campaign_goal)
- preferred_devices: tablica z: Mobile, Desktop, CTV lub null
- content_types: tablica stringów typów treści do filtrowania inventory (np. health, gaming, technology) lub null — używaj tylko jeśli brief to sugeruje wyraźniej niż sama branża
- audience_profiles: tablica (np. young_adults, mass_reach) lub null
- confidence: number od 0 do 1 — pewność co do industry i campaign_goal
- brief_summary_pl: jedno zdanie po polsku: o co chodzi w kampanii"""


def _parse_brief_with_llm(client_brief: str) -> tuple[dict, Optional[str]]:
    """Call OpenAI to extract structured fields. Returns (data, error_message)."""
    try:
        from openai import OpenAI
    except ImportError as e:
        return {}, f"openai package not installed: {e}"

    if not OPENAI_API_KEY:
        return {}, "OPENAI_API_KEY not set"

    agent_doc = _get_agent_prompt_text()
    if agent_doc.strip():
        system_content = (
            f"{agent_doc}\n\n---\n\n"
            "Jesteś backendem narzędzia MCP: z briefu klienta wyłuskujesz pola strukturalne "
            "do filtrowania inventory zgodnie z instrukcją JSON od użytkownika. "
            "W CSV kolumna kraju może nazywać się `country` lub `country_focus` (synonimy). "
            "Odpowiadasz wyłącznie jednym poprawnym obiektem JSON, bez markdown, bez tekstu poza JSON."
        )
    else:
        system_content = (
            "Jesteś analitykiem planowania mediów. Odpowiadasz tylko poprawnym JSON bez komentarzy."
        )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        user_msg = f"Brief klienta:\n\n{client_brief.strip()}\n\n{_BRIEF_JSON_INSTRUCTIONS}"
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()
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
    if key in ALLOWED_INDUSTRIES:
        return key
    return None


def _normalize_campaign_goal(raw: Optional[str]) -> str:
    if not raw or not isinstance(raw, str):
        return "awareness"
    g = raw.strip().lower()
    if g in ("awareness", "consideration", "performance"):
        return g
    return "awareness"


def _merge_brief_params(
    client_brief: str,
    budget_usd: Optional[float],
    countries: Optional[list[str]],
    campaign_goal: Optional[str],
    preferred_formats: Optional[list[str]],
    preferred_devices: Optional[list[str]],
    parsed: Optional[dict],
    llm_error: Optional[str],
) -> tuple[
    Optional[str],
    str,
    Optional[float],
    Optional[list[str]],
    Optional[list[str]],
    Optional[list[str]],
    Optional[list[str]],
    Optional[list[str]],
    dict,
]:
    """
    Merge explicit tool arguments with LLM output. Explicit non-None args win.
    Returns (industry, campaign_goal, budget, countries, formats, devices, content_types, audience_profiles, meta).
    meta: llm_used, confidence, brief_summary_pl, llm_error
    """
    meta: dict = {
        "llm_used": bool(parsed is not None and not llm_error),
        "confidence": None,
        "brief_summary_pl": None,
        "llm_error": llm_error,
    }

    if llm_error or parsed is None:
        industry = _detect_industry(client_brief)
        goal = _normalize_campaign_goal(campaign_goal)
        return (
            industry,
            goal,
            budget_usd,
            countries,
            preferred_formats,
            preferred_devices,
            None,
            None,
            meta,
        )

    meta["confidence"] = parsed.get("confidence")
    if isinstance(meta["confidence"], (int, float)):
        meta["confidence"] = float(meta["confidence"])
    else:
        meta["confidence"] = None
    bs = parsed.get("brief_summary_pl")
    meta["brief_summary_pl"] = bs if isinstance(bs, str) else None

    ind = _normalize_llm_industry(parsed.get("industry")) or _detect_industry(client_brief)
    if campaign_goal is not None:
        goal = _normalize_campaign_goal(campaign_goal)
    else:
        goal = _normalize_campaign_goal(parsed.get("campaign_goal"))

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
        if isinstance(eff_formats, list):
            eff_formats = [str(x).upper() for x in eff_formats if x]
        else:
            eff_formats = preferred_formats

    eff_devices = preferred_devices if preferred_devices is not None else parsed.get("preferred_devices")
    if eff_devices is not None and not isinstance(eff_devices, list):
        eff_devices = preferred_devices

    ct = parsed.get("content_types")
    if ct is not None and not isinstance(ct, list):
        ct = None
    ap = parsed.get("audience_profiles")
    if ap is not None and not isinstance(ap, list):
        ap = None

    mapping = INDUSTRY_MAP.get(ind.lower(), {}) if ind else {}
    if not ct and mapping.get("content"):
        ct = mapping["content"]
    if not ap and mapping.get("audience"):
        ap = mapping["audience"]

    return ind, goal, eff_budget, eff_countries, eff_formats, eff_devices, ct, ap, meta


def _placement_rationale(p: dict, industry: Optional[str]) -> str:
    parts: list[str] = []
    pt = p.get("publisher_tier", "")
    if pt:
        parts.append(f"Wydawca: tier {pt}")
    try:
        v = float(p.get("viewability", 0))
        parts.append(f"viewability {v:.0%}")
    except (TypeError, ValueError):
        pass
    ct = p.get("content_type", "")
    if ct:
        parts.append(f"typ treści: {ct}")
    if industry:
        parts.append(f"branża briefu: {industry}")
    return "; ".join(parts) if parts else "Dopasowanie według progów jakościowych i filtrów."


def _load_latest_inventory() -> tuple[pd.DataFrame, str]:
    path = os.path.join(INVENTORY_DIR, INVENTORY_FILE)
    if not os.path.isfile(path):
        pattern = os.path.join(INVENTORY_DIR, "inventory_*.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(f"No inventory_*.csv files found in {INVENTORY_DIR}")
        raise FileNotFoundError(
            f"Configured inventory file not found: {INVENTORY_FILE}. "
            f"Available files: {', '.join(os.path.basename(f) for f in files)}"
        )
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip().str.lstrip("\ufeff")
    # Newer exports use `country`; legacy files use `country_focus`
    if "country_focus" not in df.columns and "country" in df.columns:
        df["country_focus"] = df["country"]
    # Normalize string columns
    for col in ["brand_safety_risk", "publisher_tier", "line_item_type",
                "device_type", "environment", "content_type", "audience_profile",
                "geo_focus", "country_focus", "iab_category", "iab_category_secondary"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lower()
    return df, os.path.basename(path)


def _detect_industry(query: str) -> Optional[str]:
    q = query.lower()
    for key in INDUSTRY_MAP:
        if key in q:
            return key
    return None


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
    Return a high-level overview of the latest inventory:
    available formats, devices, audience profiles, IAB categories,
    country focus options, publisher tiers, and aggregate stats.
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
        "country_focus": df["country_focus"].value_counts().to_dict(),
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
                  Auto-maps to IAB categories and audience profiles.
        content_types: Filter by content type (e.g. ['health', 'sports']).
        audience_profiles: Filter by audience (e.g. ['young_adults', 'mass_reach']).
        formats: Ad formats to include (e.g. ['VIDEO', 'DISPLAY']).
        devices: Device types (e.g. ['Mobile', 'Desktop']).
        countries: Country codes (e.g. ['PL', 'US']). 'GLOBAL' always included.
        min_viewability: Minimum viewability rate (0–1). Default 0.65.
        min_quality_score: Minimum quality score (0–1). Default 0.80.
        max_brand_safety_risk: Max allowed risk: 'low' or 'medium'. Default 'low'.
        publisher_tiers: Allowed tiers (e.g. ['premium', 'mid-tier']).
        budget_usd: Max total spend in USD. Trims results to fit budget.
        limit: Max number of placements to return. Default 50.
    """
    df, filename = _load_latest_inventory()

    # Auto-map industry to content/audience if not explicitly provided
    if industry:
        mapping = INDUSTRY_MAP.get(industry.lower(), {})
        if not content_types and mapping.get("content"):
            content_types = mapping["content"]
        if not audience_profiles and mapping.get("audience"):
            audience_profiles = mapping["audience"]

    # Brand safety filter
    allowed_risk = ["low"]
    if max_brand_safety_risk == "medium":
        allowed_risk = ["low", "medium"]
    df = df[df["brand_safety_risk"].isin(allowed_risk)]

    # Quality & viewability
    df = df[df["quality_score"] >= min_quality_score]
    df = df[df["viewability"] >= min_viewability]

    # Content type
    if content_types:
        ct_lower = [c.lower() for c in content_types]
        df = df[df["content_type"].isin(ct_lower)]

    # Audience profile
    if audience_profiles:
        ap_lower = [a.lower() for a in audience_profiles]
        df = df[df["audience_profile"].isin(ap_lower)]

    # Format
    if formats:
        fmt_lower = [f.upper() for f in formats]
        df = df[df["line_item_type"].str.upper().isin(fmt_lower)]

    # Device
    if devices:
        dev_lower = [d.lower() for d in devices]
        df = df[df["device_type"].str.lower().isin(dev_lower)]

    # Country (avoid pandas edge case: boolean mask on 0-row frame can drop columns)
    if countries and len(df) > 0:
        country_upper = [c.upper() for c in countries] + ["GLOBAL"]
        df = df[df["country_focus"].apply(
            lambda x: any(c in str(x).upper() for c in country_upper)
        )]

    # Publisher tier
    if publisher_tiers:
        pt_lower = [p.lower() for p in publisher_tiers]
        df = df[df["publisher_tier"].isin(pt_lower)]

    # Sort: premium first, then by quality × cost_efficiency
    tier_order = {"premium": 0, "mid-tier": 1, "long-tail": 2}
    df["_tier_rank"] = df["publisher_tier"].map(tier_order).fillna(3)
    df["_score"] = df["quality_score"] * df["cost_efficiency_score"]
    df = df.sort_values(["_tier_rank", "_score"], ascending=[True, False])

    # Budget cap
    if budget_usd:
        df["_cumulative_spend"] = df["total_spend_usd"].cumsum()
        df = df[df["_cumulative_spend"] <= budget_usd]

    df = df.head(limit)

    cols = [
        "supply_source", "domain", "app_name", "environment", "device_type",
        "line_item_type", "creative_size", "impressions", "viewable_impressions",
        "clicks", "total_spend_usd", "viewability", "ctr", "ecpm_usd", "vcpm_usd",
        "ecpc_usd", "quality_score", "cost_efficiency_score", "iab_category",
        "content_type", "audience_profile", "geo_focus", "country_focus",
        "language", "brand_safety_risk", "publisher_tier"
    ]
    cols = [c for c in cols if c in df.columns]

    placements = df[cols].fillna("").to_dict(orient="records")

    summary = {
        "source_file": filename,
        "matched_placements": len(placements),
        "total_impressions": int(df["impressions"].sum()),
        "total_viewable_impressions": int(df["viewable_impressions"].sum()),
        "total_spend_usd": round(float(df["total_spend_usd"].sum()), 2),
        "avg_viewability": round(float(df["viewability"].mean()), 4) if len(df) else 0,
        "avg_ctr": round(float(df["ctr"].mean()), 4) if len(df) else 0,
        "avg_ecpm_usd": round(float(df["ecpm_usd"].mean()), 4) if len(df) else 0,
        "format_mix": df["line_item_type"].str.upper().value_counts().to_dict(),
        "device_mix": df["device_type"].str.lower().value_counts().to_dict(),
        "publisher_tier_mix": df["publisher_tier"].value_counts().to_dict(),
    }

    return {"summary": summary, "placements": placements}


@mcp.tool()
def parse_client_brief(client_brief: str) -> dict:
    """
    Wyciąga z briefu strukturalne parametry (OpenAI), gdy ustawione jest OPENAI_API_KEY.
    Bez klucza zwraca heurystykę (_detect_industry).
    """
    ap_loaded = bool(_get_agent_prompt_text().strip())
    if not OPENAI_API_KEY:
        return {
            "llm_available": False,
            "agent_prompt_loaded": ap_loaded,
            "heuristic_industry": _detect_industry(client_brief),
            "note": "Ustaw OPENAI_API_KEY, aby włączyć parsowanie przez model.",
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
        "normalized_industry": _normalize_llm_industry(data.get("industry"))
        or _detect_industry(client_brief),
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
    Generate a complete media plan recommendation based on a natural-language client brief.

    When OPENAI_API_KEY is set, the brief is parsed by the configured LLM (OPENAI_MODEL)
    to infer industry, goal, budget, countries, formats and devices. Explicit tool
    arguments override model output. Without a key, behavior matches legacy heuristics.

    Args:
        client_brief: Free-text description of the client and campaign
                      (e.g. 'fitness app targeting young adults in Poland').
        budget_usd: Total campaign budget in USD. Optional; overrides LLM if set.
        countries: Target country codes (e.g. ['PL']). Optional; overrides LLM if set.
        campaign_goal: 'awareness' | 'consideration' | 'performance'. Optional; if
                      omitted and LLM is enabled, inferred from the brief.
        preferred_formats: Override format selection (e.g. ['VIDEO']).
        preferred_devices: Override device selection (e.g. ['Mobile']).
        min_viewability: Minimum viewability threshold. Default 0.70.

    Returns:
        Structured media plan with selected placements, budget allocation,
        KPI projections, and strategic recommendations.
    """
    parsed: Optional[dict] = None
    llm_err: Optional[str] = None
    if OPENAI_API_KEY:
        parsed, llm_err = _parse_brief_with_llm(client_brief)

    (
        industry,
        campaign_goal_eff,
        budget_eff,
        countries_eff,
        eff_formats,
        eff_devices,
        content_types,
        audience_profiles,
        brief_meta,
    ) = _merge_brief_params(
        client_brief,
        budget_usd,
        countries,
        campaign_goal,
        preferred_formats,
        preferred_devices,
        parsed if not llm_err else None,
        llm_err,
    )

    brief_lower = client_brief.lower()

    # Resolve formats based on goal
    if eff_formats:
        formats = [f.upper() for f in eff_formats]
    elif campaign_goal_eff == "awareness":
        formats = ["VIDEO"]
    elif campaign_goal_eff == "consideration":
        formats = ["VIDEO", "DISPLAY"]
    else:
        formats = ["DISPLAY", "NATIVE"]

    # Resolve devices
    if eff_devices:
        devices = eff_devices
    elif "desktop" in brief_lower:
        devices = ["Desktop"]
    elif "ctv" in brief_lower or "tv" in brief_lower:
        devices = ["CTV", "Mobile"]
    else:
        devices = ["Mobile", "Desktop"]

    # First pass: premium placements
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
        budget_usd=budget_eff * 0.6 if budget_eff else None,
        limit=20,
    )

    remaining_budget = None
    if budget_eff:
        spent = premium_result["summary"]["total_spend_usd"]
        remaining_budget = max(0, budget_eff - spent)

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
        budget_usd=remaining_budget,
        limit=30,
    )

    all_placements = premium_result["placements"] + midtier_result["placements"]
    seen = set()
    unique_placements = []
    for p in all_placements:
        key = (p.get("domain"), p.get("app_name"), p.get("line_item_type"), p.get("creative_size"))
        if key not in seen:
            seen.add(key)
            unique_placements.append(p)

    for p in unique_placements:
        p["rationale_pl"] = _placement_rationale(p, industry)

    total_impressions = sum(p.get("impressions", 0) for p in unique_placements)
    total_viewable = sum(p.get("viewable_impressions", 0) for p in unique_placements)
    total_clicks = sum(p.get("clicks", 0) for p in unique_placements)
    total_spend = sum(p.get("total_spend_usd", 0) for p in unique_placements)
    avg_viewability = (
        sum(p.get("viewability", 0) for p in unique_placements) / len(unique_placements)
        if unique_placements else 0
    )
    avg_ctr = (
        sum(p.get("ctr", 0) for p in unique_placements) / len(unique_placements)
        if unique_placements else 0
    )

    formats_used = {}
    devices_used = {}
    for p in unique_placements:
        f = p.get("line_item_type", "UNKNOWN").upper()
        d = p.get("device_type", "unknown").lower()
        formats_used[f] = formats_used.get(f, 0) + p.get("impressions", 0)
        devices_used[d] = devices_used.get(d, 0) + p.get("impressions", 0)

    def pct_mix(counts: dict) -> dict:
        total = sum(counts.values()) or 1
        return {k: round(v / total * 100, 1) for k, v in counts.items()}

    recommendations = []
    if avg_viewability < 0.75:
        recommendations.append("Rozważ podniesienie progu viewability do 0.75 – obecna średnia jest blisko limitu.")
    if "VIDEO" not in formats_used and campaign_goal_eff == "awareness":
        recommendations.append("Dla kampanii awareness wideo jest kluczowe – rozszerz formaty o VIDEO.")
    if total_impressions > 0 and total_clicks / total_impressions < 0.003:
        recommendations.append("Niski CTR – rozważ optymalizację kreacji lub przejście na targeting performance.")
    if len(unique_placements) < 5:
        recommendations.append("Mała liczba placementów – rozszerz kryteria (więcej kategorii lub mniejszy min. quality score).")
    if budget_eff and total_spend < budget_eff * 0.7:
        recommendations.append(f"Budżet wykorzystany w {round(total_spend/budget_eff*100)}% – rozważ dodanie long-tail publisherów.")

    if not recommendations:
        recommendations.append("Plan spełnia standardy jakościowe – monitoruj viewability i CTR w trakcie kampanii.")

    _, filename = _load_latest_inventory()

    return {
        "media_plan": {
            "source_file": filename,
            "client_brief": client_brief,
            "brief_parsing": {
                "llm_used": brief_meta.get("llm_used"),
                "openai_model": OPENAI_MODEL if OPENAI_API_KEY else None,
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
                "total_impressions": total_impressions,
                "total_viewable_impressions": total_viewable,
                "total_clicks": total_clicks,
                "total_spend_usd": round(total_spend, 2),
                "budget_usd": budget_eff,
                "budget_utilization_pct": round(total_spend / budget_eff * 100, 1) if budget_eff else None,
                "avg_viewability_pct": round(avg_viewability * 100, 1),
                "avg_ctr_pct": round(avg_ctr * 100, 3),
                "avg_ecpm_usd": round(total_spend / total_impressions * 1000, 4) if total_impressions else 0,
            },
            "format_mix_pct": pct_mix(formats_used),
            "device_mix_pct": pct_mix(devices_used),
            "recommendations": recommendations,
        }
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
