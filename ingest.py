import os

import requests
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers

load_dotenv()

DATA_URL = "https://data.sf.gov/resource/38b7-6db3.json"
INDEX = "before-you-rent-dogparks"

response = requests.get(
    DATA_URL,
    params={
        "$where": "functional_area_type = 'Dog Play Area'",
        "$limit": 1000,
        "$order": "objectid",
    },
    timeout=90,
)
response.raise_for_status()
rows = response.json()

if not rows:
    raise RuntimeError("DataSF returned no records.")

documents = {}
skipped_inactive = 0

for row in rows:
    area_id = row.get("funcarea_id")
    lat, lon = row.get("latitude"), row.get("longitude")

    if not area_id or not lat or not lon:
        continue

    # West Portal is flagged "INACTIVE" in the name fields rather than a status column.
    label = f"{row.get('facility_name', '')} {row.get('area_number', '')}".upper()
    if "INACTIVE" in label:
        skipped_inactive += 1
        continue

    # Some areas (e.g. McKinley Square) are repeated under one funcarea_id.
    if area_id in documents:
        continue

    doc = {
        "funcarea_id": area_id,
        "park": row.get("property_name", ""),
        "facility": row.get("facility_name", ""),
        "area_number": row.get("area_number", ""),
        "location": {"lat": float(lat), "lon": float(lon)},
    }

    if row.get("squarefeet"):
        doc["square_feet"] = float(row["squarefeet"])
    if row.get("shape"):
        doc["shape"] = row["shape"]
    if row.get("data_as_of"):
        doc["data_as_of"] = row["data_as_of"]

    documents[area_id] = doc

print(
    f"Fetched {len(rows)} rows -> {len(documents)} dog play areas "
    f"({skipped_inactive} inactive skipped, "
    f"{len(rows) - len(documents) - skipped_inactive} duplicates/incomplete dropped)"
)

es = Elasticsearch(
    os.environ["ELASTIC_URL"],
    api_key=os.environ["ELASTIC_API_KEY"],
    request_timeout=60,
)

print("Connected:", es.info()["version"]["number"])

if not es.indices.exists(index=INDEX):
    es.indices.create(
        index=INDEX,
        mappings={
            "dynamic": False,
            "properties": {
                "funcarea_id": {"type": "keyword"},
                "park": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "facility": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "area_number": {"type": "keyword"},
                "square_feet": {"type": "float"},
                "location": {"type": "geo_point"},
                "shape": {"type": "geo_shape"},
                "data_as_of": {"type": "date"},
            },
        },
    )

indexed, _ = helpers.bulk(
    es,
    (
        {"_index": INDEX, "_id": area_id, "_source": doc}
        for area_id, doc in documents.items()
    ),
)

es.indices.refresh(index=INDEX)

print(f"Indexed {indexed} records")
print("Total documents:", es.count(index=INDEX)["count"])

print("\nSample dog play areas:")
for doc in sorted(documents.values(), key=lambda d: d["park"])[:10]:
    print(f"{doc['park']} | {doc['facility']} | {doc['location']['lat']}, {doc['location']['lon']}")
