import os
import glob
import json
from typing import Optional
import pandas as pd
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("adlook-inventory-recommender")

INVENTORY_DIR = os.environ.get("INVENTORY_DIR", ".")

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


def _load_latest_inventory() -> tuple[pd.DataFrame, str]:
    pattern = os.path.join(INVENTORY_DIR, "inventory_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No inventory_*.csv files found in {INVENTORY_DIR}")
    path = files[-1]
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip().str.lstrip("\ufeff")
    # Normalize string columns
    for col in ["brand_safety_risk", "publisher_tier", "line_item_type",
                "device_type", "environment", "content_type", "audience_profile",
                "geo_focus", "iab_category", "iab_category_secondary"]:
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

    # Country
    if countries:
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
def create_media_plan(
    client_brief: str,
    budget_usd: Optional[float] = None,
    countries: Optional[list[str]] = None,
    campaign_goal: str = "awareness",
    preferred_formats: Optional[list[str]] = None,
    preferred_devices: Optional[list[str]] = None,
    min_viewability: float = 0.70,
) -> dict:
    """
    Generate a complete media plan recommendation based on a natural-language client brief.

    Args:
        client_brief: Free-text description of the client and campaign
                      (e.g. 'fitness app targeting young adults in Poland').
        budget_usd: Total campaign budget in USD. Optional.
        countries: Target country codes (e.g. ['PL']). Optional.
        campaign_goal: 'awareness' | 'consideration' | 'performance'. Default 'awareness'.
        preferred_formats: Override format selection (e.g. ['VIDEO']).
        preferred_devices: Override device selection (e.g. ['Mobile']).
        min_viewability: Minimum viewability threshold. Default 0.70.

    Returns:
        Structured media plan with selected placements, budget allocation,
        KPI projections, and strategic recommendations.
    """
    brief_lower = client_brief.lower()

    # Detect industry
    industry = _detect_industry(client_brief)
    mapping = INDUSTRY_MAP.get(industry, {}) if industry else {}

    # Resolve formats based on goal
    if preferred_formats:
        formats = [f.upper() for f in preferred_formats]
    elif campaign_goal == "awareness":
        formats = ["VIDEO"]
    elif campaign_goal == "consideration":
        formats = ["VIDEO", "DISPLAY"]
    else:  # performance
        formats = ["DISPLAY", "NATIVE"]

    # Resolve devices
    if preferred_devices:
        devices = preferred_devices
    elif "desktop" in brief_lower:
        devices = ["Desktop"]
    elif "ctv" in brief_lower or "tv" in brief_lower:
        devices = ["CTV", "Mobile"]
    else:
        devices = ["Mobile", "Desktop"]

    # Resolve audience
    audience_profiles = mapping.get("audience") or None
    content_types = mapping.get("content") or None

    # Publisher tier strategy: premium-first for awareness/consideration
    if campaign_goal == "performance":
        publisher_tiers = ["premium", "mid-tier", "long-tail"]
    else:
        publisher_tiers = ["premium", "mid-tier"]

    # First pass: premium placements
    premium_result = find_placements(
        industry=industry,
        content_types=content_types,
        audience_profiles=audience_profiles,
        formats=formats,
        devices=devices,
        countries=countries,
        min_viewability=min_viewability,
        min_quality_score=0.85,
        max_brand_safety_risk="low",
        publisher_tiers=["premium"],
        budget_usd=budget_usd * 0.6 if budget_usd else None,
        limit=20,
    )

    # Second pass: mid-tier to fill remaining budget
    remaining_budget = None
    if budget_usd:
        spent = premium_result["summary"]["total_spend_usd"]
        remaining_budget = max(0, budget_usd - spent)

    midtier_result = find_placements(
        industry=industry,
        content_types=content_types,
        audience_profiles=audience_profiles,
        formats=formats,
        devices=devices,
        countries=countries,
        min_viewability=min_viewability - 0.05,
        min_quality_score=0.80,
        max_brand_safety_risk="low",
        publisher_tiers=["mid-tier"],
        budget_usd=remaining_budget,
        limit=30,
    )

    # Merge and deduplicate placements
    all_placements = premium_result["placements"] + midtier_result["placements"]
    seen = set()
    unique_placements = []
    for p in all_placements:
        key = (p.get("domain"), p.get("app_name"), p.get("line_item_type"), p.get("creative_size"))
        if key not in seen:
            seen.add(key)
            unique_placements.append(p)

    # Aggregate KPIs
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

    # Strategic recommendations
    recommendations = []
    if avg_viewability < 0.75:
        recommendations.append("Rozważ podniesienie progu viewability do 0.75 – obecna średnia jest blisko limitu.")
    if "VIDEO" not in formats_used and campaign_goal == "awareness":
        recommendations.append("Dla kampanii awareness wideo jest kluczowe – rozszerz formaty o VIDEO.")
    if total_impressions > 0 and total_clicks / total_impressions < 0.003:
        recommendations.append("Niski CTR – rozważ optymalizację kreacji lub przejście na targeting performance.")
    if len(unique_placements) < 5:
        recommendations.append("Mała liczba placementów – rozszerz kryteria (więcej kategorii lub mniejszy min. quality score).")
    if budget_usd and total_spend < budget_usd * 0.7:
        recommendations.append(f"Budżet wykorzystany w {round(total_spend/budget_usd*100)}% – rozważ dodanie long-tail publisherów.")

    if not recommendations:
        recommendations.append("Plan spełnia standardy jakościowe – monitoruj viewability i CTR w trakcie kampanii.")

    _, filename = _load_latest_inventory()

    return {
        "media_plan": {
            "source_file": filename,
            "client_brief": client_brief,
            "detected_industry": industry,
            "campaign_goal": campaign_goal,
            "formats_requested": formats,
            "devices_requested": devices,
            "countries": countries or ["GLOBAL"],
            "placements_count": len(unique_placements),
            "placements": unique_placements,
            "kpis": {
                "total_impressions": total_impressions,
                "total_viewable_impressions": total_viewable,
                "total_clicks": total_clicks,
                "total_spend_usd": round(total_spend, 2),
                "budget_usd": budget_usd,
                "budget_utilization_pct": round(total_spend / budget_usd * 100, 1) if budget_usd else None,
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
    port = int(os.environ.get("PORT", 8000))
    os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")
    os.environ.setdefault("FASTMCP_PORT", str(port))
    mcp.run(transport="streamable-http")
