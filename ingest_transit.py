import os

import requests
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers

load_dotenv()

DATA_URL = "https://data.sf.gov/resource/i28k-bkz6.json"
INDEX = "before-you-rent-muni"

response = requests.get(
    DATA_URL,
    params={"$limit": 10000, "$order": "stopid"},
    timeout=90,
)
response.raise_for_status()
rows = response.json()

if not rows:
    raise RuntimeError("DataSF returned no records.")

documents = {}

for row in rows:
    stop_id = row.get("stopid")
    lat, lon = row.get("latitude"), row.get("longitude")

    if not stop_id or not lat or not lon:
        continue

    if stop_id in documents:
        continue

    documents[stop_id] = {
        "stop_id": stop_id,
        "stop_name": row.get("stopname", ""),
        "on_street": row.get("onstreet", ""),
        "at_street": row.get("atstreet", ""),
        "location": {"lat": float(lat), "lon": float(lon)},
    }

print(
    f"Fetched {len(rows)} rows -> {len(documents)} Muni stops "
    f"({len(rows) - len(documents)} duplicates/incomplete dropped)"
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
                "stop_id": {"type": "keyword"},
                "stop_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "on_street": {"type": "keyword"},
                "at_street": {"type": "keyword"},
                "location": {"type": "geo_point"},
            },
        },
    )

indexed, _ = helpers.bulk(
    es,
    (
        {"_index": INDEX, "_id": stop_id, "_source": doc}
        for stop_id, doc in documents.items()
    ),
)

es.indices.refresh(index=INDEX)

print(f"Indexed {indexed} records")
print("Total documents:", es.count(index=INDEX)["count"])
