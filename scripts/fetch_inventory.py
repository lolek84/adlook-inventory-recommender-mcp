#!/usr/bin/env python3
"""
Fetch inventory from adlook Reporting MCP (https://adlook-mcp.onrender.com/sse).

Strategy to bypass 10K row limit:
  - Use result_filters to filter IMPRESSIONS > 1000 server-side (reduces result set)
  - Use output_format=csv for compact transfer
  - If result count near 10K, split by country and aggregate
"""
import json
import csv
import io
import time
import threading
import urllib.request
from datetime import date, timedelta

MCP_SSE_URL = "https://adlook-mcp.onrender.com/sse"
ACCESS_TOKEN = "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzc4OTQxOTk5LCJpYXQiOjE3Nzg5NDE2OTksImp0aSI6ImViZDViZDJkMDFkMjQ3ZjE4MGE4MDNmMTlhZjQ3MmI1IiwidXNlcl91dWlkIjoiNjBjODE1ZTgtN2QwNS00NWZhLTg0ZjktMDMyNzhiZTc4Mjc1IiwicGVybWlzc2lvbnMiOlsiY3JlYXRpdmUucmVhZCIsImN1c3RvbV9yZXBvcnQuY3JlYXRlIiwiYXVkaWVuY2UucmVhZCIsInJlcG9ydC5yZWFkIiwiY3VzdG9tX3JlcG9ydC5kZWxldGUiLCJhZ2VuY3lfYXVkaWVuY2UucmVhZCIsImFnZW5jeS5yZWFkIiwiY2FtcGFpZ24ucmVhZCIsImN1c3RvbV9yZXBvcnQudXBkYXRlIiwiaW50ZXJuYWxfdXNlci5yZWFkIiwiZXh0ZXJuYWxfdXNlci5yZWFkIiwiYWR2ZXJ0aXNlcl9jaGFuZ2VzZXQucmVhZCIsImJyYW5kX3NhZmV0eV90ZW1wbGF0ZS5yZWFkIiwiY2hhbmdlbG9nLnJlYWQiLCJsaW5lX2l0ZW1fY2hhbmdlc2V0LnJlYWQiLCJhZHZlcnRpc2VyLnJlYWQiXX0.PyDgDZLx5IZN6eIsKWQkaaSa5QrYHhp9V7XxxtelQ28STCJO_qANgt3nuTAye3uf3EQjEta1Xg13vRkchaZCZA"
REFRESH_TOKEN = "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoicmVmcmVzaCIsImV4cCI6MTc3OTAyODA5OSwiaWF0IjoxNzc4OTQxNjk5LCJqdGkiOiJmYWY4MjI5NzJiZDQ0NDFmOTA4YzExYmQ3OGRiMjUwNiIsInVzZXJfdXVpZCI6IjYwYzgxNWU4LTdkMDUtNDVmYS04NGY5LTAzMjc4YmU3ODI3NSIsInBlcm1pc3Npb25zIjpbImNyZWF0aXZlLnJlYWQiLCJjdXN0b21fcmVwb3J0LmNyZWF0ZSIsImF1ZGllbmNlLnJlYWQiLCJyZXBvcnQucmVhZCIsImN1c3RvbV9yZXBvcnQuZGVsZXRlIiwiYWdlbmN5X2F1ZGllbmNlLnJlYWQiLCJhZ2VuY3kucmVhZCIsImNhbXBhaWduLnJlYWQiLCJjdXN0b21fcmVwb3J0LnVwZGF0ZSIsImludGVybmFsX3VzZXIucmVhZCIsImV4dGVybmFsX3VzZXIucmVhZCIsImFkdmVydGlzZXJfY2hhbmdlc2V0LnJlYWQiLCJicmFuZF9zYWZldHlfdGVtcGxhdGUucmVhZCIsImNoYW5nZWxvZy5yZWFkIiwibGluZV9pdGVtX2NoYW5nZXNldC5yZWFkIiwiYWR2ZXJ0aXNlci5yZWFkIl19.h5xl4eMBw8p74aB67Gz2HnP9C_iYj9xMWbamr_86kVuu_zz4RodEcwUyZ0cyiTp6v2Z0AAnKXfoRSgLpcXS-KQ"

TODAY = date.today()
OUTPUT_FILE = "inventory_db_20260515.csv"
MIN_IMPRESSIONS = 100
LIMIT_THRESHOLD = 9500  # if near 10K, split

DIMENSIONS = [
    "SUPPLY_SOURCE", "DOMAIN", "APP_NAME", "DEVICE_TYPE", "ENVIRONMENT",
    "LINE_ITEM_TYPE", "CREATIVE_SIZE", "COUNTRY",
]
METRICS = [
    "IMPRESSIONS", "VIEWABLE_IMPRESSIONS", "MEASURABLE_IMPRESSIONS",
    "VIEWABILITY", "MEASURABILITY", "CLICKS", "CLICK_THROUGH_RATE",
    "TOTAL_SPEND_USD", "ECPM_USD", "VCPM_USD", "ECPC_USD",
    "VIDEO_COMPLETE_VIEWS", "VIDEO_COMPLETION_RATE",
]


# ---------------------------------------------------------------------------
# MCP SSE session
# ---------------------------------------------------------------------------

class MCPSession:
    def __init__(self, sse_url):
        self.sse_url = sse_url
        self.message_url = None
        self._responses = {}
        self._lock = threading.Lock()
        self._stop = False
        self._connect()

    def _connect(self):
        req = urllib.request.Request(self.sse_url, headers={"Accept": "text/event-stream"})
        self._conn = urllib.request.urlopen(req, timeout=30)
        et = None
        dl = []
        while True:
            raw = self._conn.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip()
            if line.startswith("event:"):
                et = line[6:].strip()
            elif line.startswith("data:"):
                dl.append(line[5:].strip())
            elif line == "":
                if et == "endpoint" and dl:
                    self.message_url = self.sse_url.rsplit("/", 1)[0] + dl[0]
                    print(f"  Sesja: {dl[0]}")
                    break
                et = None
                dl = []
        threading.Thread(target=self._read_sse, daemon=True).start()

    def _read_sse(self):
        et = None
        dl = []
        while not self._stop:
            try:
                raw = self._conn.readline()
                if not raw:
                    break
                line = raw.decode("utf-8").rstrip()
                if line.startswith("event:"):
                    et = line[6:].strip()
                elif line.startswith("data:"):
                    dl.append(line[5:].strip())
                elif line == "":
                    if et == "message" and dl:
                        try:
                            msg = json.loads("\n".join(dl))
                            if msg.get("id") is not None:
                                with self._lock:
                                    self._responses[msg["id"]] = msg
                        except Exception:
                            pass
                    et = None
                    dl = []
            except Exception:
                break

    def call(self, method, params, req_id, timeout=120):
        body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
        urllib.request.urlopen(
            urllib.request.Request(
                self.message_url, data=body,
                headers={"Content-Type": "application/json"}, method="POST"
            ), timeout=30
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if req_id in self._responses:
                    return self._responses.pop(req_id)
            time.sleep(0.05)
        raise TimeoutError(f"Brak odpowiedzi na request {req_id}")

    def close(self):
        self._stop = True


_req_id = 0


def next_id():
    global _req_id
    _req_id += 1
    return _req_id


def call_tool(session, name, args, timeout=180):
    resp = session.call("tools/call", {"name": name, "arguments": args}, req_id=next_id(), timeout=timeout)
    content = resp.get("result", {}).get("content", [])
    if not content:
        raise ValueError(f"Pusta odpowiedź dla {name}: {resp}")
    text = content[0].get("text", "")
    if "MCP error" in text or text.startswith("Błąd"):
        raise ValueError(f"Błąd narzędzia {name}: {text[:500]}")
    return text


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def parse_response(text):
    """
    API returns JSON wrapper: {"status": "SUCCEEDED", "result": "CSV_OR_JSON_STRING"}.
    Extract result field and parse. Result may be CSV string or JSON string.
    """
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "result" in obj:
            inner = obj["result"]
            if isinstance(inner, list):
                # Already a list of dicts
                return inner
            if isinstance(inner, str):
                # Try JSON first
                try:
                    parsed = json.loads(inner)
                    if isinstance(parsed, list):
                        return parsed
                except (json.JSONDecodeError, Exception):
                    pass
                # Parse as CSV
                reader = csv.DictReader(io.StringIO(inner))
                return list(reader)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, Exception):
        pass
    # Last resort: raw CSV
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def run_report(session, dimensions, metrics, period=None, start_date=None, end_date=None,
               result_filters=None, country=None, label=""):
    """Run a single report and return list of dicts."""
    args = {
        "dimensions": dimensions,
        "metrics": metrics,
        "output_format": "csv",
        "sort": {"column": "IMPRESSIONS", "direction": "DESC"},
    }
    if period:
        args["period"] = period
    else:
        args["start_date"] = start_date
        args["end_date"] = end_date

    # Server-side filter: IMPRESSIONS > 100
    base_filter = {"column": "IMPRESSIONS", "gt": MIN_IMPRESSIONS}

    if country:
        country_filter = {"column": "COUNTRY", "regexp": f"^{country}$"}
        args["result_filters"] = {"and": [base_filter, country_filter]}
    else:
        args["result_filters"] = base_filter

    print(f"  Raport {label} ...", end=" ", flush=True)
    text = call_tool(session, "run_report_preview", args, timeout=180)
    rows = parse_response(text)
    print(f"{len(rows)} wierszy")
    return rows


def get_distinct_countries(session):
    """Get list of all countries present in last 30 days."""
    print("  Pobieranie listy krajów...", end=" ", flush=True)
    args = {
        "dimensions": ["COUNTRY"],
        "metrics": ["IMPRESSIONS"],
        "period": "last_30_days",
        "result_filters": {"column": "IMPRESSIONS", "gt": 0},
        "output_format": "csv",
    }
    text = call_tool(session, "run_report_preview", args, timeout=120)
    rows = parse_response(text)
    if rows and not isinstance(rows[0], dict):
        print(f"\n  DEBUG raw: {text[:400]}")
        raise ValueError(f"Nieoczekiwany typ wiersza: {type(rows[0])}")
    countries = [r["COUNTRY"] for r in rows if r.get("COUNTRY")]
    print(f"{len(countries)} krajów: {countries}")
    return countries


def fetch_all(session, limit_hit=False):
    """
    Fetch all inventory with >1000 impressions from last 30 days.
    Splits by country if API returned exactly 10K rows (limit hit).
    """
    if not limit_hit:
        # No limit issue — single query with server-side filter
        rows = run_report(
            session,
            dimensions=DIMENSIONS,
            metrics=METRICS,
            period="last_30_days",
            label="last_30_days (pełny)"
        )
        return rows

    # API hit 10K limit — must split by country to get complete data
    print(f"  ⚠ API zwróciło dokładnie 10K wierszy — dzielę po krajach dla kompletności")
    countries = get_distinct_countries(session)

    all_rows = {}
    for i, country in enumerate(countries, 1):
        country_rows = run_report(
            session,
            dimensions=DIMENSIONS,
            metrics=METRICS,
            period="last_30_days",
            country=country,
            label=f"kraj {country} [{i}/{len(countries)}]"
        )
        for r in country_rows:
            key = tuple(r.get(d, "") for d in DIMENSIONS)
            if key not in all_rows:
                all_rows[key] = r
            else:
                # aggregate numeric metrics
                for m in METRICS:
                    if m in ("VIEWABILITY", "MEASURABILITY", "CLICK_THROUGH_RATE",
                             "VIDEO_COMPLETION_RATE", "ECPM_USD", "VCPM_USD", "ECPC_USD"):
                        continue  # skip rates/ratios
                    try:
                        all_rows[key][m] = float(all_rows[key].get(m) or 0) + float(r.get(m) or 0)
                    except (ValueError, TypeError):
                        pass

    return list(all_rows.values())


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

CSV_FIELDNAMES = [
    "SUPPLY_SOURCE", "DOMAIN", "APP_NAME", "DEVICE_TYPE", "ENVIRONMENT",
    "LINE_ITEM_TYPE", "CREATIVE_SIZE", "COUNTRY",
    "IMPRESSIONS", "VIEWABLE_IMPRESSIONS", "MEASURABLE_IMPRESSIONS",
    "VIEWABILITY", "CLICKS", "CLICK_THROUGH_RATE",
    "TOTAL_SPEND_USD", "ECPM_USD", "VCPM_USD", "ECPC_USD",
    "VIDEO_COMPLETE_VIEWS", "VIDEO_COMPLETION_RATE",
    "measurability_rate",
]


def to_output_row(r):
    def pct(v):
        """Normalize to 'XX.XX%' string. Input may be '80.75%', '0.8075', or 80.75."""
        if v is None or str(v).strip() == "":
            return "0.00%"
        s = str(v).strip()
        if s.endswith("%"):
            try:
                return f"{float(s[:-1]):.2f}%"
            except (ValueError, TypeError):
                return s
        try:
            f = float(s)
            return f"{f:.2f}%" if f > 1.0 else f"{f * 100:.2f}%"
        except (ValueError, TypeError):
            return s

    def pct_as_decimal(v):
        """Parse '80.75%' → 0.8075 for measurability_rate column."""
        if v is None or str(v).strip() == "":
            return 0.0
        s = str(v).strip()
        if s.endswith("%"):
            try:
                return round(float(s[:-1]) / 100, 4)
            except (ValueError, TypeError):
                return 0.0
        try:
            f = float(s)
            return round(f if f <= 1.0 else f / 100, 4)
        except (ValueError, TypeError):
            return 0.0

    return {
        "SUPPLY_SOURCE": r.get("SUPPLY_SOURCE", ""),
        "DOMAIN": r.get("DOMAIN", ""),
        "APP_NAME": r.get("APP_NAME", ""),
        "DEVICE_TYPE": r.get("DEVICE_TYPE", ""),
        "ENVIRONMENT": r.get("ENVIRONMENT", ""),
        "LINE_ITEM_TYPE": r.get("LINE_ITEM_TYPE", ""),
        "CREATIVE_SIZE": r.get("CREATIVE_SIZE", ""),
        "COUNTRY": r.get("COUNTRY", ""),
        "IMPRESSIONS": int(float(r.get("IMPRESSIONS") or 0)),
        "VIEWABLE_IMPRESSIONS": int(float(r.get("VIEWABLE_IMPRESSIONS") or 0)),
        "MEASURABLE_IMPRESSIONS": int(float(r.get("MEASURABLE_IMPRESSIONS") or 0)),
        "VIEWABILITY": pct(r.get("VIEWABILITY")),
        "CLICKS": int(float(r.get("CLICKS") or 0)),
        "CLICK_THROUGH_RATE": pct(r.get("CLICK_THROUGH_RATE")),
        "TOTAL_SPEND_USD": round(float(r.get("TOTAL_SPEND_USD") or 0), 6),
        "ECPM_USD": round(float(r.get("ECPM_USD") or 0), 3),
        "VCPM_USD": round(float(r.get("VCPM_USD") or 0), 3),
        "ECPC_USD": round(float(r.get("ECPC_USD") or 0), 3),
        "VIDEO_COMPLETE_VIEWS": int(float(r.get("VIDEO_COMPLETE_VIEWS") or 0)),
        "VIDEO_COMPLETION_RATE": pct(r.get("VIDEO_COMPLETION_RATE")),
        "measurability_rate": pct_as_decimal(r.get("MEASURABILITY")),
    }


def main():
    print(f"Łączenie z {MCP_SSE_URL} ...")
    session = MCPSession(MCP_SSE_URL)

    # Authenticate
    print("Autoryzacja...")
    auth_text = call_tool(session, "set_adlook_auth", {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
    })
    print(f"  {auth_text[:80]}")

    # Quick count without filter to detect API limit hit
    print(f"\nSprawdzam łączną liczbę wierszy (bez filtra)...")
    total_check = call_tool(session, "run_report_preview", {
        "dimensions": DIMENSIONS,
        "metrics": ["IMPRESSIONS"],
        "period": "last_30_days",
        "output_format": "csv",
    }, timeout=120)
    total_rows_check = parse_response(total_check)
    total_count = len(total_rows_check)
    print(f"  Łączna liczba wierszy (bez filtra IMPRESSIONS): {total_count}")
    limit_hit = total_count >= LIMIT_THRESHOLD

    # Fetch data
    print(f"\nPobieranie danych (ostatnie 30 dni, filter: IMPRESSIONS > {MIN_IMPRESSIONS})...")
    rows = fetch_all(session, limit_hit=limit_hit)
    session.close()

    print(f"\nŁącznie: {len(rows)} wierszy")

    # Sort by impressions desc
    rows.sort(key=lambda r: int(float(r.get("IMPRESSIONS") or 0)), reverse=True)

    # Write output CSV
    output_rows = [to_output_row(r) for r in rows]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Zapisano: {OUTPUT_FILE} ({len(output_rows)} wierszy)")

    # Show top 5
    print("\nTop 5 wg impresji:")
    for r in output_rows[:5]:
        domain = r["DOMAIN"] or r["APP_NAME"]
        print(f"  {domain:40s} {r['COUNTRY']:4s} {int(r['IMPRESSIONS']):>10,} impresji")


if __name__ == "__main__":
    main()
