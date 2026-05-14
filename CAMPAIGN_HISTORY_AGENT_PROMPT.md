# Inicjalizacja bazy historii kampanii — Prompt dla agenta

## Cel

Podłącz się do serwera MCP Adlook (`https://adlook-mcp.onrender.com/sse`) i pobierz dane
o wynikach kampanii z podziałem **per reklamodawca × placement**. Zapisz wyniki jako plik CSV
gotowy do zasilenia narzędzia `get_campaign_insights` w serwerze inventory-recommender.

---

## Wymagania wstępne

Przed pierwszym zapytaniem wywołaj `check_auth`. Jeśli token wygasł — poproś użytkownika
o podanie `access_token` (JWT z przeglądarki) i wywołaj `set_adlook_auth`.

---

## Kroki

### 1. Pobierz dane per advertiser × placement

Wywołaj `run_report_preview` z następującymi parametrami:

```
dimensions: [
  ADVERTISER_NAME,
  SUPPLY_SOURCE, DOMAIN, APP_NAME, DEVICE_TYPE,
  ENVIRONMENT, LINE_ITEM_TYPE, CREATIVE_SIZE, COUNTRY
]
metrics: [
  IMPRESSIONS, VIEWABLE_IMPRESSIONS, MEASURABLE_IMPRESSIONS,
  VIEWABILITY, CLICKS, CLICK_THROUGH_RATE,
  TOTAL_SPEND_USD, ECPM_USD, VCPM_USD, ECPC_USD,
  VIDEO_COMPLETE_VIEWS, VIDEO_COMPLETION_RATE
]
period: last_30_days
result_filters: { column: IMPRESSIONS, gte: 500 }
sort: { column: IMPRESSIONS, direction: DESC }
output_format: csv
```

> **Uwaga:** Próg 500 impresji (niższy niż w inventory_db) zapewnia pokrycie kampanii
> niszowych. Limit 10K wierszy API jest wystarczający przy sortowaniu DESC.

---

### 2. Wylicz metryki pochodne

Dla każdego wiersza wylicz poniższe kolumny (te same co w `MEDIA_PLANNER_AGENT_PROMPT.md`):

#### Metryki jakościowe

```
effective_viewability = VIEWABLE_IMPRESSIONS / IMPRESSIONS
# Jeśli IMPRESSIONS = 0, ustaw 0.

measurability_rate = MEASURABLE_IMPRESSIONS / IMPRESSIONS
# Jeśli IMPRESSIONS = 0, ustaw 0.

cost_per_viewable_impression = TOTAL_SPEND_USD / VIEWABLE_IMPRESSIONS * 1000
# Jeśli VIEWABLE_IMPRESSIONS = 0, ustaw null.
```

#### Quality Score (0–1)

```
quality_score = (
  0.40 × VIEWABILITY +
  0.35 × measurability_rate +
  0.25 × VIDEO_COMPLETION_RATE
)
# Dla wierszy bez danych video:
# quality_score = 0.50 × VIEWABILITY + 0.50 × measurability_rate
# Zaokrąglij do 4 miejsc po przecinku.
```

#### Cost Efficiency Score (0–1)

```
cost_efficiency_raw = VIEWABLE_IMPRESSIONS / TOTAL_SPEND_USD
# Jeśli TOTAL_SPEND_USD = 0, ustaw null.

cost_efficiency_score = normalizacja min-max (cost_efficiency_raw) w ramach całego datasetu
# Zaokrąglij do 4 miejsc po przecinku.
```

#### Brand Safety Score (0–1) i poziom ryzyka

```
brand_safety_score = (
  0.50 × measurability_rate +
  0.50 × VIEWABILITY
)
# Korekta −0.20 jeśli CLICK_THROUGH_RATE > 0.005 ORAZ VIEWABILITY < 0.50.
brand_safety_score = max(0, brand_safety_score − korekcja)
# Zaokrąglij do 4 miejsc po przecinku.

brand_safety_risk =
  "low"    jeśli brand_safety_score >= 0.70
  "medium" jeśli brand_safety_score >= 0.40
  "high"   jeśli brand_safety_score <  0.40
```

---

### 3. Klasyfikacja branżowa reklamodawców

Dla każdego unikalnego `ADVERTISER_NAME` przypisz:

- `advertiser_industry` — jedna z: fitness, sport, health, food, fmcg, automotive, finance,
  banking, ecommerce, retail, tech, gaming, fashion, beauty, education, family, news,
  entertainment, travel, other
- `iab_category` — główna kategoria IAB odpowiadająca branży (np. IAB7 dla health/fitness)
- `iab_category_secondary` — opcjonalna kategoria dodatkowa
- `content_type` — jeden z: health, sports, finance, automotive, technology, gaming,
  entertainment, news, lifestyle, education, parenting, shopping, other
- `audience_profile` — jeden lub więcej z: young_adults, mass_reach, professionals,
  high_income, parents, students, tech_enthusiasts (rozdzielone przecinkami)

Klasyfikację wykonaj wsadowo (batch po 20 reklamodawców) przez Claude Haiku z promptem:

```
Dla każdego reklamodawcy z listy przypisz branżę (advertiser_industry), główną kategorię
IAB (iab_category), content_type i audience_profile (może być wiele, rozdziel przecinkami).
Odpowiedz wyłącznie jako JSON array: [{"name": "...", "advertiser_industry": "...",
"iab_category": "...", "iab_category_secondary": "...", "content_type": "...",
"audience_profile": "..."}, ...]
```

Dla niejednoznacznych reklamodawców ustaw `advertiser_industry = "other"`.

---

### 4. Zapisz jako CSV

Zapisz wynik do pliku `campaign_history_db_YYYYMMDD.csv` z kolumnami:

```
ADVERTISER_NAME, advertiser_industry,
SUPPLY_SOURCE, DOMAIN, APP_NAME, DEVICE_TYPE, ENVIRONMENT,
LINE_ITEM_TYPE, CREATIVE_SIZE, COUNTRY,
IMPRESSIONS, VIEWABLE_IMPRESSIONS, MEASURABLE_IMPRESSIONS,
VIEWABILITY, CLICKS, CLICK_THROUGH_RATE,
TOTAL_SPEND_USD, ECPM_USD, VCPM_USD, ECPC_USD,
VIDEO_COMPLETE_VIEWS, VIDEO_COMPLETION_RATE,
effective_viewability, measurability_rate, cost_per_viewable_impression,
quality_score, cost_efficiency_score,
brand_safety_score, brand_safety_risk,
iab_category, iab_category_secondary, content_type, audience_profile
```

Plik powinien trafić do tego samego katalogu co `inventory_db_*.csv` (INVENTORY_DIR na Render).

---

### 5. Potwierdź wynik

Po zapisaniu wyświetl podsumowanie:

- nazwa pliku i ścieżka
- łączna liczba wierszy
- liczba unikalnych reklamodawców
- top 10 reklamodawców wg IMPRESSIONS
- rozkład `advertiser_industry`
- rozkład `COUNTRY` (top 10)
- rozkład `DEVICE_TYPE` i `ENVIRONMENT`
- łączne IMPRESSIONS i TOTAL_SPEND_USD

---

## Jak używać w serwerze MCP

Po wgraniu pliku na Render ustaw zmienną środowiskową:

```
HISTORY_FILE = campaign_history_db_YYYYMMDD.csv
```

Narzędzie `get_campaign_insights` automatycznie wczyta plik i wzbogaci odpowiedzi
o dane per-advertiser (sekcja `history` w odpowiedzi).
