# Inicjalizacja bazy danych inventory — Prompt dla agenta

## Cel

Podłącz się do serwera MCP Adlook (`https://adlook-mcp.onrender.com/sse`) i pobierz dane o rzeczywistych wynikach kampanii z podziałem na placements (domeny, aplikacje, supply sources). Zapisz wyniki jako plik CSV gotowy do analizy i budowy media planów.

---

## Wymagania wstępne

Przed pierwszym zapytaniem wywołaj `check_auth`. Jeśli token wygasł lub brak tokenu — poproś użytkownika o podanie `access_token` (JWT z przeglądarki) i wywołaj `set_adlook_auth`.

---

## Kroki

### 1. Pobierz dane inventory per placement

Wywołaj `run_report_preview` z następującymi parametrami:

```
dimensions: [
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
result_filters: { column: IMPRESSIONS, gte: 1000 }
sort: { column: IMPRESSIONS, direction: DESC }
output_format: csv
```

### 2. Wylicz metryki pochodne

Dla każdego wiersza wylicz poniższe kolumny i dołącz je do danych przed zapisem.

#### Metryki jakościowe

```
effective_viewability = VIEWABLE_IMPRESSIONS / IMPRESSIONS
# Bardziej konserwatywna niż VIEWABILITY (która dzieli przez MEASURABLE_IMPRESSIONS).
# Jeśli IMPRESSIONS = 0, ustaw 0.

measurability_rate = MEASURABLE_IMPRESSIONS / IMPRESSIONS
# Jak wiarygodne są dane viewability. Poniżej 0.4 → sygnał ostrzegawczy.
# Jeśli IMPRESSIONS = 0, ustaw 0.

cost_per_viewable_impression = TOTAL_SPEND_USD / VIEWABLE_IMPRESSIONS * 1000
# vCPM wyliczony ręcznie — weryfikacja VCPM_USD i porównanie między placementami.
# Jeśli VIEWABLE_IMPRESSIONS = 0, ustaw null.
```

#### Quality Score (0–1)

```
quality_score = (
  0.40 × VIEWABILITY +
  0.35 × measurability_rate +
  0.25 × VIDEO_COMPLETION_RATE
)
# Dla wierszy bez danych video (VIDEO_COMPLETION_RATE = 0 lub null):
# quality_score = 0.50 × VIEWABILITY + 0.50 × measurability_rate
# Zaokrąglij do 4 miejsc po przecinku.
```

#### Cost Efficiency Score (0–1)

```
cost_efficiency_raw = VIEWABLE_IMPRESSIONS / TOTAL_SPEND_USD
# Ile viewable impresji kupujesz za dolara.
# Jeśli TOTAL_SPEND_USD = 0, ustaw null.

cost_efficiency_score = normalizacja min-max (cost_efficiency_raw) w ramach całego datasetu
# 1.0 = najefektywniejszy placement, 0.0 = najdroższy.
# Zaokrąglij do 4 miejsc po przecinku.
```

#### Brand Safety Score (0–1) i poziom ryzyka

```
brand_safety_score = (
  0.50 × measurability_rate +
  0.50 × VIEWABILITY
)

# Korekta w dół (−0.20) jeśli jednocześnie:
#   CLICK_THROUGH_RATE > 0.005 (0.5%) ORAZ VIEWABILITY < 0.50
# → anomalia wskazująca na potencjalny ruch fraudowy lub MFA (Made for Advertising).
# Minimum po korekcie: 0.

brand_safety_score = max(0, brand_safety_score − korekcja)
# Zaokrąglij do 4 miejsc po przecinku.

brand_safety_risk =
  "low"    jeśli brand_safety_score >= 0.70
  "medium" jeśli brand_safety_score >= 0.40
  "high"   jeśli brand_safety_score <  0.40
```

### 3. Zapisz jako CSV

Zapisz wynik do pliku `inventory_db_YYYYMMDD.csv` z następującymi kolumnami (surowe dane + wyliczone metryki):

```
SUPPLY_SOURCE, DOMAIN, APP_NAME, DEVICE_TYPE, ENVIRONMENT,
LINE_ITEM_TYPE, CREATIVE_SIZE, COUNTRY,
IMPRESSIONS, VIEWABLE_IMPRESSIONS, MEASURABLE_IMPRESSIONS,
VIEWABILITY, CLICKS, CLICK_THROUGH_RATE,
TOTAL_SPEND_USD, ECPM_USD, VCPM_USD, ECPC_USD,
VIDEO_COMPLETE_VIEWS, VIDEO_COMPLETION_RATE,
effective_viewability, measurability_rate, cost_per_viewable_impression,
quality_score, cost_efficiency_score,
brand_safety_score, brand_safety_risk
```

### 4. Potwierdź wynik


Po zapisaniu wyświetl podsumowanie:
- nazwa pliku i ścieżka
- liczba wierszy (placements z ≥ 1 000 impresji)
- zakres dat (`last_30_days` = jakie konkretne daty)
- top 5 domen/aplikacji wg IMPRESSIONS
- rozkład COUNTRY (top 10 krajów wg IMPRESSIONS)
- rozkład DEVICE_TYPE i ENVIRONMENT
- łączne IMPRESSIONS i TOTAL_SPEND_USD
