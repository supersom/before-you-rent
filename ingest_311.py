import argparse
import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers

load_dotenv()

DATA_URL = "https://data.sf.gov/resource/vw6y-z8j6.json"
INDEX = "before-you-rent-311"
PAGE_SIZE = 20000

parser = argparse.ArgumentParser(description="Load recent 311 cases into Elasticsearch.")
parser.add_argument("--days", type=int, default=90, help="how many days back to load (default 90)")
args = parser.parse_args()

since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%dT00:00:00")

es = Elasticsearch(
    os.environ["ELASTIC_URL"],
    api_key=os.environ["ELASTIC_API_KEY"],
    request_timeout=120,
)

print("Connected:", es.info()["version"]["number"])

if not es.indices.exists(index=INDEX):
    es.indices.create(
        index=INDEX,
        mappings={
            "dynamic": False,
            "properties": {
                "service_request_id": {"type": "keyword"},
                "requested_datetime": {"type": "date"},
                "closed_date": {"type": "date"},
                "status": {"type": "keyword"},
                "status_notes": {"type": "text"},
                "agency": {"type": "keyword"},
                "service_name": {"type": "keyword"},
                "service_subtype": {"type": "keyword"},
                "service_details": {"type": "text"},
                "address": {"type": "keyword"},
                "neighborhood": {"type": "keyword"},
                "source": {"type": "keyword"},
                "location": {"type": "geo_point"},
            },
        },
    )


def to_doc(row):
    case_id = row.get("service_request_id")
    lat, lon = row.get("lat"), row.get("long")

    # Cases without coordinates cannot be matched to an address by distance.
    if not case_id or not lat or not lon:
        return None

    doc = {
        "service_request_id": case_id,
        "requested_datetime": row.get("requested_datetime"),
        "status": row.get("status_description", "Unknown"),
        "status_notes": row.get("status_notes", ""),
        "agency": row.get("agency_responsible", ""),
        "service_name": row.get("service_name", ""),
        "service_subtype": row.get("service_subtype", ""),
        "service_details": row.get("service_details", ""),
        "address": row.get("address", ""),
        "neighborhood": row.get("analysis_neighborhood", ""),
        "source": row.get("source", ""),
        "location": {"lat": float(lat), "lon": float(lon)},
    }

    if row.get("closed_date"):
        doc["closed_date"] = row["closed_date"]

    return doc


print(f"Loading 311 cases requested since {since[:10]} into {INDEX}")

fetched = indexed_total = skipped = 0
offset = 0

while True:
    response = requests.get(
        DATA_URL,
        params={
            "$select": (
                "service_request_id,requested_datetime,closed_date,status_description,"
                "status_notes,agency_responsible,service_name,service_subtype,"
                "service_details,address,analysis_neighborhood,lat,long,source"
            ),
            "$where": f"requested_datetime >= '{since}'",
            "$order": "requested_datetime, service_request_id",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        },
        timeout=120,
    )
    response.raise_for_status()
    rows = response.json()

    if not rows:
        break

    docs = [d for d in map(to_doc, rows) if d]
    skipped += len(rows) - len(docs)
    fetched += len(rows)

    indexed, _ = helpers.bulk(
        es,
        (
            {"_index": INDEX, "_id": d["service_request_id"], "_source": d}
            for d in docs
        ),
    )
    indexed_total += indexed
    offset += PAGE_SIZE
    print(f"  fetched {fetched:,} · indexed {indexed_total:,}")

    if len(rows) < PAGE_SIZE:
        break

if fetched == 0:
    raise RuntimeError("DataSF returned no records.")

es.indices.refresh(index=INDEX)

print(f"\nFetched {fetched:,} rows, skipped {skipped:,} without coordinates")
print("Total documents:", f"{es.count(index=INDEX)['count']:,}")
