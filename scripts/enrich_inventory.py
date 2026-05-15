#!/usr/bin/env python3
"""
Enrich inventory_db CSV with derived metrics and LLM-based domain classification.

Computed fields (from raw data):
  effective_viewability, cost_per_viewable_impression, quality_score,
  cost_efficiency_score, brand_safety_score

LLM-classified fields (per unique domain, via Claude API, batched):
  iab_category, iab_category_secondary, content_type, audience_profile,
  brand_safety_risk, placement_rationale_pl
"""
import csv
import json
import os
import sys
import time
import math
import urllib.request
from datetime import date

INPUT_FILE = f"inventory_db_{date.today().strftime('%Y%m%d')}.csv"
OUTPUT_FILE = INPUT_FILE  # overwrite in place

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 30  # domains per LLM call

OUTPUT_FIELDNAMES = [
    "SUPPLY_SOURCE", "DOMAIN", "APP_NAME", "DEVICE_TYPE", "ENVIRONMENT",
    "LINE_ITEM_TYPE", "CREATIVE_SIZE", "COUNTRY",
    "IMPRESSIONS", "VIEWABLE_IMPRESSIONS", "MEASURABLE_IMPRESSIONS",
    "VIEWABILITY", "CLICKS", "CLICK_THROUGH_RATE",
    "TOTAL_SPEND_USD", "ECPM_USD", "VCPM_USD", "ECPC_USD",
    "VIDEO_COMPLETE_VIEWS", "VIDEO_COMPLETION_RATE",
    "effective_viewability", "measurability_rate", "cost_per_viewable_impression",
    "quality_score", "cost_efficiency_score", "brand_safety_score",
    "brand_safety_risk", "iab_category", "iab_category_secondary",
    "content_type", "audience_profile", "placement_rationale_pl",
]


# ---------------------------------------------------------------------------
# Computed metrics
# ---------------------------------------------------------------------------

def safe_float(v, default=0.0):
    try:
        return float(str(v).strip().rstrip("%")) if v not in (None, "", "nan") else default
    except (ValueError, TypeError):
        return default


def pct_to_float(v):
    s = str(v or "").strip()
    if s.endswith("%"):
        return safe_float(s[:-1]) / 100
    f = safe_float(s)
    return f / 100 if f > 1.5 else f


def compute_metrics(row):
    impressions = safe_float(row.get("IMPRESSIONS"))
    viewable = safe_float(row.get("VIEWABLE_IMPRESSIONS"))
    measurable = safe_float(row.get("MEASURABLE_IMPRESSIONS"))
    spend = safe_float(row.get("TOTAL_SPEND_USD"))
    ecpm = safe_float(row.get("ECPM_USD"))
    vcpm = safe_float(row.get("VCPM_USD"))

    # effective_viewability = viewable / impressions
    eff_view = round(viewable / impressions, 4) if impressions > 0 else 0.0

    # measurability_rate = measurable / impressions
    meas_rate = round(measurable / impressions, 4) if impressions > 0 else 0.0

    # cost_per_viewable_impression = spend / viewable * 1000
    if viewable > 0:
        cpvi = round(spend / viewable * 1000, 4)
    elif vcpm > 0:
        cpvi = round(vcpm, 4)
    else:
        cpvi = 0.0

    # quality_score: weighted blend of effective_viewability and measurability
    if meas_rate == 0.0 and eff_view == 0.0:
        quality = 0.0
    else:
        quality = round((eff_view * 0.65 + meas_rate * 0.35), 4)

    # cost_efficiency_score: inverse of normalized ECPM (low ECPM = higher efficiency)
    if ecpm > 0:
        cost_eff = round(1.0 / (ecpm * 10000), 4)
    else:
        cost_eff = 0.0

    return {
        "effective_viewability": eff_view,
        "measurability_rate": meas_rate,
        "cost_per_viewable_impression": cpvi,
        "quality_score": quality,
        "cost_efficiency_score": cost_eff,
    }


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def claude_classify_batch(domains: list[str], api_key: str) -> dict:
    """
    Classify a batch of domains/app names.
    Returns dict: {domain -> {iab_category, iab_category_secondary, content_type,
                              audience_profile, brand_safety_risk, placement_rationale_pl}}
    """
    domain_list = "\n".join(f"{i+1}. {d}" for i, d in enumerate(domains))

    prompt = f"""Sklasyfikuj poniższe domeny/aplikacje reklamowe. Dla każdej zwróć JSON z polami:
- iab_category: główna kategoria IAB (np. "IAB12 - News & Politics")
- iab_category_secondary: opcjonalna kategoria drugorzędna (lub "")
- content_type: jeden z: news, sports, entertainment, lifestyle, health, finance, technology, automotive, education, gaming, travel, other
- audience_profile: lista segmentów (rozdzielona przecinkami) spośród: mass_reach, young_adults, professionals, high_income, parents, students, tech_enthusiasts
- brand_safety_risk: "low", "medium" lub "high"
- placement_rationale_pl: 1 zdanie po polsku opisujące dlaczego ta domena jest wartościowa dla reklamodawców

Odpowiedz TYLKO prawidłowym JSON array (bez komentarzy):
[
  {{"domain": "example.com", "iab_category": "...", "iab_category_secondary": "...", "content_type": "...", "audience_profile": "...", "brand_safety_risk": "...", "placement_rationale_pl": "..."}},
  ...
]

Domeny do klasyfikacji:
{domain_list}"""

    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
                text = result["content"][0]["text"].strip()
                # Extract JSON array from response
                start = text.find("[")
                end = text.rfind("]") + 1
                if start >= 0 and end > start:
                    classifications = json.loads(text[start:end])
                    return {item["domain"]: item for item in classifications}
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            print(f"  BŁĄD klasyfikacji batcha: {e}")
    return {}


def classify_domains(unique_domains: list[str], api_key: str) -> dict:
    """Classify all unique domains in batches, return {domain -> classification}."""
    results = {}
    batches = [unique_domains[i:i + BATCH_SIZE] for i in range(0, len(unique_domains), BATCH_SIZE)]
    total = len(batches)

    for i, batch in enumerate(batches, 1):
        print(f"  Klasyfikuję batch {i}/{total} ({len(batch)} domen)...", end=" ", flush=True)
        batch_result = claude_classify_batch(batch, api_key)
        results.update(batch_result)
        print(f"OK ({len(batch_result)} sklasyfikowanych)")
        if i < total:
            time.sleep(0.5)  # gentle rate limiting

    return results


def default_classification(domain: str) -> dict:
    """Fallback when LLM unavailable."""
    return {
        "iab_category": "IAB12 - News & Politics",
        "iab_category_secondary": "",
        "content_type": "other",
        "audience_profile": "mass_reach",
        "brand_safety_risk": "low",
        "placement_rationale_pl": f"Serwis {domain} dociera do szerokiej grupy odbiorców.",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Nie znaleziono pliku: {INPUT_FILE}")
        sys.exit(1)

    print(f"Wczytuję: {INPUT_FILE}")
    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"  {len(rows)} wierszy")

    # Compute numeric metrics
    print("Obliczam metryki...")
    for row in rows:
        computed = compute_metrics(row)
        row.update(computed)

    # Gather unique domain identifiers for LLM classification
    unique_domains = sorted(set(
        (row["DOMAIN"] or row["APP_NAME"]).strip()
        for row in rows
        if (row["DOMAIN"] or row["APP_NAME"]).strip()
    ))
    print(f"Unikalnych domen/apek do klasyfikacji: {len(unique_domains)}")

    # LLM classification
    if ANTHROPIC_API_KEY:
        print(f"Klasyfikuję przez Claude API ({CLAUDE_MODEL})...")
        domain_classifications = classify_domains(unique_domains, ANTHROPIC_API_KEY)
        print(f"  Sklasyfikowano: {len(domain_classifications)}/{len(unique_domains)}")
    else:
        print("UWAGA: brak ANTHROPIC_API_KEY — używam domyślnych klasyfikacji")
        domain_classifications = {}

    # Apply classifications to rows + compute brand_safety_score
    missing = 0
    for row in rows:
        domain_key = (row["DOMAIN"] or row["APP_NAME"]).strip()
        classification = domain_classifications.get(domain_key) or default_classification(domain_key)
        if domain_key not in domain_classifications:
            missing += 1

        row["iab_category"] = classification.get("iab_category", "")
        row["iab_category_secondary"] = classification.get("iab_category_secondary", "")
        row["content_type"] = classification.get("content_type", "other")
        row["audience_profile"] = classification.get("audience_profile", "mass_reach")
        row["brand_safety_risk"] = classification.get("brand_safety_risk", "low")
        row["placement_rationale_pl"] = classification.get("placement_rationale_pl", "")

        # brand_safety_score: quality_score for low/medium, 0 for high
        risk = row["brand_safety_risk"]
        quality = float(row.get("quality_score") or 0)
        if risk == "high":
            row["brand_safety_score"] = 0.0
        elif risk == "medium":
            row["brand_safety_score"] = round(quality * 0.75, 4)
        else:
            row["brand_safety_score"] = quality

    if missing:
        print(f"  {missing} domen z klasyfikacją domyślną")

    # Write output
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nZapisano: {OUTPUT_FILE} ({len(rows)} wierszy)")
    print(f"Kolumny: {OUTPUT_FIELDNAMES}")

    # Quick stats
    risks = {}
    for row in rows:
        r = row.get("brand_safety_risk", "?")
        risks[r] = risks.get(r, 0) + 1
    print(f"\nBrand safety: {risks}")

    content_types = {}
    for row in rows:
        c = row.get("content_type", "?")
        content_types[c] = content_types.get(c, 0) + 1
    print(f"Content types: {dict(sorted(content_types.items(), key=lambda x: -x[1]))}")


if __name__ == "__main__":
    main()
