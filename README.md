# Before You Rent

A Streamlit app for people deciding whether to rent at a San Francisco address. Type an address and it shows:

- **Dog play areas** within 1 km (0.6 mi), nearest first, with a map.
- **Muni stops** within 1 km: the true count, with the 5 nearest listed and mapped.
- **311 cases** within 50 m of the address, from the last 90 days, with a category breakdown.
- **A report for prospective tenants**, written by a Mistral model from all three of the above, with questions to ask the landlord.

Everything comes from [data.sf.gov](https://data.sf.gov). The data is loaded into Elasticsearch ahead of time, and the app queries it by distance.

## How it works

| Piece | Source dataset | Elasticsearch index | Loaded by |
|---|---|---|---|
| Dog play areas | Functional Areas maintained by Recreation and Parks (`38b7-6db3`), filtered to `Dog Play Area` | `before-you-rent-dogparks` | `ingest.py` |
| Muni stops | Muni Stops (`i28k-bkz6`) | `before-you-rent-muni` | `ingest_transit.py` |
| 311 cases | 311 Cases (`vw6y-z8j6`) | `before-you-rent-311` | `ingest_311.py` |
| Address lookup | San Francisco Addresses, Enterprise Addressing System (`3mea-di5p`) | none (queried live) | `app.py` |

The address you type is matched against the city's own address dataset to get coordinates, so no third-party geocoder is used. Dog parks, stops, and cases are then found with Elasticsearch `geo_distance` queries.

## Prerequisites

- Python 3.12 (developed on 3.12.4).
- An Elasticsearch deployment. This was built against Elastic Cloud Serverless. The API key needs to create indices, write documents, and read them.
- A Mistral API key.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create a `.env` file in the project root. It is gitignored; never commit it.

```
ELASTIC_URL=https://your-deployment.es.region.gcp.elastic.cloud
ELASTIC_API_KEY=your-elastic-api-key
MISTRAL_API_KEY=your-mistral-api-key
MISTRAL_MODEL=mistral-large-latest
```

`MISTRAL_MODEL` is optional and defaults to `mistral-small-latest`. Any chat model your Mistral account can use will work. Pinning a dated ID such as `mistral-large-2512` keeps reports reproducible, because `-latest` aliases can move.

## Loading the data

Run each ingest once. All three are safe to re-run, since documents use the source ID as `_id` and are updated in place.

```bash
.venv/bin/python ingest.py                  # dog play areas (~40 rows)
.venv/bin/python ingest_transit.py          # Muni stops (~3,300 rows)
.venv/bin/python ingest_311.py --days 90    # 311 cases (~225,000 rows for 90 days)
```

Notes on the ingests:

- **`ingest.py`** skips areas marked inactive and collapses duplicate rows that share an ID (McKinley Square appears five times in the source).
- **`ingest_311.py`** loads only a recent window, not the full 9M-row dataset. `--days` sets the window (default 90). Cases without coordinates are skipped, because they cannot be matched to an address by distance. Loading 90 days took roughly 10 minutes; 7 days took under a minute, which is handy for a quick trial (`--days 7`).
- **311 data is a snapshot.** Nothing refreshes it. Re-running `ingest_311.py` adds newer cases and updates existing ones, but it does not delete cases that fall outside the window, so the index grows over time.

## Running the app

```bash
.venv/bin/streamlit run app.py
```

Then open http://localhost:8501. To have the page re-run automatically when you save a file, add `--server.runOnSave true`.

Changes to `.env`, such as switching `MISTRAL_MODEL`, need a restart, because the values are read once at startup.

## Using the app

1. Type a San Francisco street address, starting with the number (for example `845 California`), and press **Search**.
2. If several addresses match, pick one from the list.
3. Press **Generate report** for the AI summary. The dog park, Muni, and 311 sections appear below it.

## Limitations

- **Address search starts from the street number.** `845 California` works; `California St` alone does not.
- **Distances are straight-line**, not walking routes. Hills, freeways, and fences can make real trips longer.
- **311 cases are public reports placed near the address by coordinates.** They are not necessarily about the building, can overlap, and a "Closed" status does not mean a problem was fixed. Only the loaded window is covered.
- **Dog parks** are those maintained by SF Recreation and Parks. "None nearby" does not mean nowhere to take a dog.
- **Muni stops** are individual stops, one per direction, so the count is not a count of lines. The data has no routes or service frequency, and **BART is not included**, since data.sf.gov has no BART station locations.
- **The report is AI-generated** and can be wrong. It cannot tell you the condition of a building. The app only shows cited 311 cases that exist in the data it supplied to the model, but the wording is the model's own.

## Privacy and cost

Generating a report sends the selected address, the nearby 311 case text, and the names and distances of nearby dog parks and Muni stops to Mistral. The 311 and city data are public, but the address is whatever the user typed. Each report is one Mistral API call, and larger models cost more per call.

## Project layout

```
app.py              Streamlit app: search, results, report
report.py           Mistral prompt, output parsing, citation checks
ingest.py           Dog play areas -> Elasticsearch
ingest_transit.py   Muni stops -> Elasticsearch
ingest_311.py       311 cases -> Elasticsearch
requirements.txt    Pinned direct dependencies
```
