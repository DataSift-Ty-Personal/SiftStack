"""Discover Tracerfy response field names — test both batch and instant endpoints."""
import csv
import io
import json
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "src")
import config as cfg

API_KEY = cfg.TRACERFY_API_KEY
if not API_KEY:
    print("ERROR: TRACERFY_API_KEY not set in .env")
    sys.exit(1)

# Test record
first_name = "Dwight"
last_name = "Thacker"
address = "1942 Example Tops Ln"
city = "Seymour"
state = "TN"
zip_code = "37865"

print(f"Testing: {first_name} {last_name}, {address}, {city} {state} {zip_code}")

# ── Test 1: Instant Trace (single-record, synchronous) ──
print("\n" + "=" * 60)
print("TEST 1: Instant Trace (POST /v1/api/trace/lookup/)")
print("=" * 60)
try:
    resp = requests.post(
        "https://tracerfy.com/v1/api/trace/lookup/",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "address": address,
            "city": city,
            "state": state,
            "zip": zip_code,
            "find_owner": False,
            "first_name": first_name,
            "last_name": last_name,
        },
        timeout=30,
    )
    print(f"Status: {resp.status_code}")
    data = resp.json()
    print(f"Hit: {data.get('hit')}")
    print(f"Credits deducted: {data.get('credits_deducted')}")
    print(f"Persons count: {data.get('persons_count')}")
    if data.get("persons"):
        for i, p in enumerate(data["persons"]):
            print(f"\n--- Person {i} ---")
            print(f"Keys: {sorted(p.keys())}")
            print(json.dumps(p, indent=2, default=str))
    else:
        print("No persons returned")
        print(f"Full response: {json.dumps(data, indent=2)}")
except Exception as e:
    print(f"ERROR: {e}")

# ── Test 2: Batch Trace (CSV, async polling) ──
print("\n" + "=" * 60)
print("TEST 2: Batch Trace (POST /v1/api/trace/)")
print("=" * 60)
try:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["first_name", "last_name", "address", "city", "state", "zip",
                "mail_address", "mail_city", "mail_state"])
    w.writerow([first_name, last_name, address, city, state, zip_code, "", "", ""])
    csv_content = buf.getvalue()
    buf.close()

    resp = requests.post(
        "https://tracerfy.com/v1/api/trace/",
        headers={"Authorization": f"Bearer {API_KEY}"},
        data={
            "first_name_column": "first_name",
            "last_name_column": "last_name",
            "address_column": "address",
            "city_column": "city",
            "state_column": "state",
            "zip_column": "zip",
            "mail_address_column": "mail_address",
            "mail_city_column": "mail_city",
            "mail_state_column": "mail_state",
            "mailing_zip_column": "zip",
        },
        files={"csv_file": ("discovery.csv", csv_content, "text/csv")},
        timeout=15,
    )
    print(f"Submit status: {resp.status_code}")
    queue_data = resp.json()
    queue_id = queue_data.get("queue_id")
    est_wait = queue_data.get("estimated_wait_seconds", "?")
    print(f"Queue ID: {queue_id}, Est wait: {est_wait}s")

    if not queue_id:
        print(f"Full response: {json.dumps(queue_data, indent=2)}")
    else:
        # Poll for results
        for attempt in range(20):
            time.sleep(3)
            result_resp = requests.get(
                f"https://tracerfy.com/v1/api/queue/{queue_id}",
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=15,
            )
            result_data = result_resp.json()

            if isinstance(result_data, list):
                records = result_data
            elif isinstance(result_data, dict):
                status = result_data.get("status", "")
                print(f"  Poll {attempt+1}: status={status}")
                if status == "failed":
                    break
                if status != "completed":
                    continue
                records = result_data.get("records", [])
            else:
                continue

            print(f"\n=== BATCH RESPONSE ({len(records)} records) ===")
            for i, rec in enumerate(records):
                print(f"\n--- Record {i} ---")
                print(f"Keys: {sorted(rec.keys())}")
                print(json.dumps(rec, indent=2, default=str))
            break
        else:
            print("Timed out after 60s")
except Exception as e:
    print(f"ERROR: {e}")

print("\n" + "=" * 60)
print("DONE — check field names above to verify they match the code")
print("Expected batch fields: primary_phone, mobile_1..5, landline_1..3, email_1..5")
print("=" * 60)
