import math
import os
import re

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from elasticsearch import Elasticsearch

from report import generate_report

load_dotenv()

PARKS_INDEX = "before-you-rent-dogparks"
CASES_INDEX = "before-you-rent-311"
STOPS_INDEX = "before-you-rent-muni"
RADIUS_M = 1000
STOPS_SHOWN = 5
CASE_RADIUS_M = 50
METERS_PER_MILE = 1609.344
ADDRESS_URL = "https://data.sf.gov/resource/3mea-di5p.json"
PARKS_SOURCE_URL = "https://data.sf.gov/d/38b7-6db3"
CASES_SOURCE_URL = "https://data.sf.gov/d/vw6y-z8j6"
STOPS_SOURCE_URL = "https://data.sf.gov/d/i28k-bkz6"

st.set_page_config(
    page_title="Before You Rent",
    page_icon="🐕",
    layout="centered",
)


ORDINAL = re.compile(r"0*(\d+)(ST|ND|RD|TH)")
SPECIAL_WORDS = {"OFARRELL": "O'Farrell", "O'FARRELL": "O'Farrell"}


def pretty_word(word):
    upper = word.upper()
    ordinal = ORDINAL.fullmatch(upper)

    if ordinal:
        return f"{int(ordinal[1])}{ordinal[2].lower()}"
    if upper in SPECIAL_WORDS:
        return SPECIAL_WORDS[upper]
    if "-" in word:
        return "-".join(pretty_word(part) for part in word.split("-"))
    if upper.startswith("MC") and len(upper) > 3 and upper[2].isalpha():
        return f"Mc{upper[2]}{upper[3:].lower()}"
    return word.capitalize()


def pretty(text):
    # str.title() turns "6TH" into "6Th" and "MCALLISTER" into "Mcallister".
    return " ".join(pretty_word(word) for word in text.split())


@st.cache_resource
def get_client():
    return Elasticsearch(
        os.environ["ELASTIC_URL"],
        api_key=os.environ["ELASTIC_API_KEY"],
        request_timeout=60,
    )


@st.cache_data(ttl=300)
def search_addresses(prefix):
    safe = prefix.upper().replace("'", "''").replace("%", "").replace("_", "")
    response = requests.get(
        ADDRESS_URL,
        params={
            "$select": "address,zip_code,latitude,longitude",
            "$where": f"upper(address) like '{safe}%'",
            "$group": "address,zip_code,latitude,longitude",
            "$order": "address",
            "$limit": 25,
        },
        timeout=30,
    )
    response.raise_for_status()
    return [
        {
            "label": f"{row['address']}, San Francisco {row.get('zip_code', '')}".strip(),
            "lat": float(row["latitude"]),
            "lon": float(row["longitude"]),
        }
        for row in response.json()
        if row.get("latitude") and row.get("longitude")
    ]


@st.cache_data(ttl=86400)
def nearest_address(lat, lon):
    # The dog park data has coordinates only, so borrow the closest street address.
    try:
        response = requests.get(
            ADDRESS_URL,
            params={
                "$select": "address,zip_code",
                "$where": f"within_circle(point, {lat}, {lon}, 250)",
                "$order": f"distance_in_meters(point, 'POINT({lon} {lat})')",
                "$limit": 1,
            },
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except Exception:
        return None

    if not rows:
        return None
    return f"{pretty(rows[0]['address'])}, San Francisco {rows[0].get('zip_code', '')}".strip()


@st.cache_data(ttl=300)
def get_dog_parks(lat, lon):
    result = get_client().search(
        index=PARKS_INDEX,
        query={
            "bool": {
                "filter": {
                    "geo_distance": {
                        "distance": f"{RADIUS_M}m",
                        "location": {"lat": lat, "lon": lon},
                    }
                }
            }
        },
        sort=[
            {
                "_geo_distance": {
                    "location": {"lat": lat, "lon": lon},
                    "order": "asc",
                    "unit": "m",
                }
            }
        ],
        size=50,
        track_total_hits=True,
    )

    return [
        {**hit["_source"], "distance_m": hit["sort"][0]}
        for hit in result["hits"]["hits"]
    ]


@st.cache_data(ttl=300)
def get_stops(lat, lon):
    result = get_client().search(
        index=STOPS_INDEX,
        query={
            "bool": {
                "filter": {
                    "geo_distance": {
                        "distance": f"{RADIUS_M}m",
                        "location": {"lat": lat, "lon": lon},
                    }
                }
            }
        },
        sort=[
            {
                "_geo_distance": {
                    "location": {"lat": lat, "lon": lon},
                    "order": "asc",
                    "unit": "m",
                }
            }
        ],
        size=STOPS_SHOWN,
        track_total_hits=True,
    )

    return {
        "total": result["hits"]["total"]["value"],
        "records": [
            {**hit["_source"], "distance_m": hit["sort"][0]}
            for hit in result["hits"]["hits"]
        ],
    }


@st.cache_data(ttl=300)
def get_cases(lat, lon):
    result = get_client().search(
        index=CASES_INDEX,
        query={
            "bool": {
                "filter": {
                    "geo_distance": {
                        "distance": f"{CASE_RADIUS_M}m",
                        "location": {"lat": lat, "lon": lon},
                    }
                }
            }
        },
        sort=[
            {"requested_datetime": {"order": "desc", "missing": "_last"}},
            {"service_request_id": "asc"},
        ],
        size=100,
        track_total_hits=True,
        aggs={
            "categories": {
                "terms": {"field": "service_name", "size": 15},
            }
        },
    )

    return {
        "total": result["hits"]["total"]["value"],
        "records": [hit["_source"] for hit in result["hits"]["hits"]],
        "categories": {
            b["key"]: b["doc_count"]
            for b in result["aggregations"]["categories"]["buckets"]
        },
    }


@st.cache_data(ttl=300)
def get_case_window():
    result = get_client().search(
        index=CASES_INDEX,
        size=0,
        aggs={
            "earliest": {"min": {"field": "requested_datetime"}},
            "latest": {"max": {"field": "requested_datetime"}},
        },
    )
    aggs = result["aggregations"]
    return (
        (aggs["earliest"].get("value_as_string") or "")[:10],
        (aggs["latest"].get("value_as_string") or "")[:10],
    )


def miles(meters):
    return meters / METERS_PER_MILE


def map_frame(choice, markers, color):
    # st.map sizes circles in meters but zooms to fit the points with no
    # margin, so the default swamps a tight map and edge points get clipped.
    # Scale the markers with the spread, and add two invisible corner points
    # so the view is centered on the address with room around the farthest one.
    farthest = max(distance for _, _, distance in markers)
    radius = max(6, 0.035 * farthest)
    half = 1.4 * max(farthest, 60)
    dlat = half / 111_320
    dlon = half / (111_320 * math.cos(math.radians(choice["lat"])))

    points = [
        {"lat": choice["lat"], "lon": choice["lon"], "color": "#d62728", "size": radius * 1.5}
    ]
    points += [
        {"lat": lat, "lon": lon, "color": color, "size": radius}
        for lat, lon, _ in markers
    ]
    points += [
        {"lat": choice["lat"] + sign * dlat, "lon": choice["lon"] + sign * dlon,
         "color": "#00000000", "size": 1}
        for sign in (-1, 1)
    ]

    # st.map's automatic zoom is too coarse, so derive it: meters per pixel at
    # zoom z is 78271.5 * cos(lat) / 2**z, and the map is 500 px tall.
    meters_per_pixel = 2 * half * 1.1 / 500
    zoom = round(math.log2(78_271.5 * math.cos(math.radians(choice["lat"])) / meters_per_pixel))
    return pd.DataFrame(points), max(10, min(zoom, 18))


def display_date(value):
    return value[:10] if value else "Unknown"


def show_dog_parks(choice, parks):
    st.subheader("Dog play areas")
    st.metric(
        f"Within {miles(RADIUS_M):.1f} miles ({RADIUS_M / 1000:g} km)",
        len(parks),
    )

    st.warning(
        "Distances are straight-line from the address, not walking routes, "
        "so a hill, freeway, or fence line can make the real trip longer. "
        "Only parks maintained by SF Recreation & Parks are included; "
        "none nearby does not mean there is nowhere to take a dog."
    )

    if not parks:
        st.info(f"No dog play areas found within {RADIUS_M / 1000:g} km.")
        return

    frame, zoom = map_frame(
        choice,
        [(p["location"]["lat"], p["location"]["lon"], p["distance_m"]) for p in parks],
        "#2ca02c",
    )
    st.map(frame, latitude="lat", longitude="lon", color="color", size="size", zoom=zoom)
    st.caption("Red: the address. Green: dog play areas.")

    st.markdown("**Nearest first**")

    for park in parks:
        lat, lon = park["location"]["lat"], park["location"]["lon"]
        near = nearest_address(lat, lon)
        distance = f"{miles(park['distance_m']):.2f} mi"

        with st.expander(f"{distance} · {park['facility'] or park['park']}"):
            st.write(f"**{park['facility'] or park['park']}**")
            st.write(f"Park: {park['park']}")
            if near:
                st.write(f"Near: {near}")
                st.caption(
                    "Closest street address to the play area’s center, "
                    "so treat it as approximate."
                )
            if park.get("area_number"):
                st.caption(f"Area: {park['area_number']}")
            if park.get("square_feet"):
                st.caption(f"Size: {park['square_feet']:,.0f} sq ft")
            st.caption(
                f"Straight-line distance: {park['distance_m']:,.0f} m "
                f"from {pretty(choice['label'].split(',')[0])}"
            )
            st.link_button(
                "Open in Google Maps",
                f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
            )

    st.link_button("Open the dog play area dataset", PARKS_SOURCE_URL)


def stop_label(stop):
    if stop.get("on_street") and stop.get("at_street"):
        return f"{pretty(stop['on_street'])} at {pretty(stop['at_street'])}"
    return stop.get("stop_name") or "Unnamed stop"


def show_stops(choice, stops):
    st.subheader("Muni stops")
    st.metric(
        f"Within {miles(RADIUS_M):.1f} miles ({RADIUS_M / 1000:g} km)",
        stops["total"],
    )

    st.warning(
        "Distances are straight-line from the address, not walking routes. "
        "This covers Muni stops only (bus and Muni Metro, one entry per "
        "direction), not BART, and says nothing about routes or how often "
        "service runs."
    )

    if not stops["records"]:
        st.info(f"No Muni stops found within {RADIUS_M / 1000:g} km.")
        return

    frame, zoom = map_frame(
        choice,
        [(s["location"]["lat"], s["location"]["lon"], s["distance_m"]) for s in stops["records"]],
        "#1f77b4",
    )
    st.map(frame, latitude="lat", longitude="lon", color="color", size="size", zoom=zoom)
    st.caption("Red: the address. Blue: Muni stops.")

    st.markdown("**Nearest first**")
    st.caption(
        f"Showing {len(stops['records'])} of {stops['total']:,} stops within "
        f"{RADIUS_M / 1000:g} km."
    )

    for stop in stops["records"]:
        lat, lon = stop["location"]["lat"], stop["location"]["lon"]
        distance = f"{miles(stop['distance_m']):.2f} mi"

        with st.expander(f"{distance} · {stop_label(stop)}"):
            st.write(f"**{stop_label(stop)}**")
            st.caption(f"Stop name in the data: {stop.get('stop_name') or 'Unknown'}")
            st.caption(f"Stop ID: {stop['stop_id']}")
            st.caption(f"Straight-line distance: {stop['distance_m']:,.0f} m")
            st.link_button(
                "Open in Google Maps",
                f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
            )

    st.link_button("Open the Muni stops dataset", STOPS_SOURCE_URL)


def show_cases(cases, window):
    st.subheader("311 cases near this address")
    st.metric(
        f"Within {CASE_RADIUS_M} m, {window[0]} to {window[1]}",
        cases["total"],
    )

    st.warning(
        "311 cases are reports from the public, placed near this address by "
        "coordinates. They are not necessarily about this building, may "
        "overlap or repeat, and “Closed” does not necessarily mean the "
        f"problem was fixed. Only requests from {window[0]} to {window[1]} "
        "are loaded, so this is not a complete history."
    )

    if not cases["records"]:
        st.info(f"No 311 cases found within {CASE_RADIUS_M} m in this period.")
        return

    st.markdown("**By category**")
    st.dataframe(
        pd.DataFrame(
            {"Category": list(cases["categories"]), "Cases": list(cases["categories"].values())}
        ),
        hide_index=True,
        width="stretch",
    )

    st.markdown("**Newest first**")
    st.caption(
        f"Showing {min(len(cases['records']), 25)} of {cases['total']:,} matching cases."
    )

    for record in cases["records"][:25]:
        filed = display_date(record.get("requested_datetime"))
        category = record.get("service_name") or "Unknown"
        status = record.get("status") or "Unknown"
        case_id = record["service_request_id"]

        with st.expander(f"{filed} · {category} · {status} · #{case_id}"):
            if record.get("service_subtype"):
                st.caption(f"Type: {record['service_subtype']}")
            notes = " ".join(
                part
                for part in (record.get("status_notes"), record.get("service_details"))
                if part
            )
            st.text(notes or "No notes recorded.")
            st.caption(f"Agency: {record.get('agency') or 'Unknown'}")
            st.caption(f"Reported address: {record.get('address') or 'Unknown'}")
            st.caption(f"Source case number: {case_id}")

    st.link_button("Open the original 311 dataset", CASES_SOURCE_URL)


def show_report(choice, cases, parks, stops, window):
    st.subheader("Report for prospective tenants")

    selected_cases = cases["records"][:40] if cases else []
    nearest_stops = [
        {"name": stop_label(s), "distance_m": s["distance_m"]}
        for s in stops["records"]
    ]
    st.caption(
        "The AI report uses the category counts, the "
        f"{len(selected_cases)} most recent 311 cases within "
        f"{CASE_RADIUS_M} m, the nearby dog play areas, and the "
        f"{len(nearest_stops)} nearest Muni stops. "
        "Each case’s notes are limited to 500 characters."
    )

    report_key = (
        choice["label"],
        tuple(c["service_request_id"] for c in selected_cases),
        tuple((p["funcarea_id"], round(p["distance_m"])) for p in parks),
        stops["total"],
        tuple((s["name"], round(s["distance_m"])) for s in nearest_stops),
    )

    if st.button("Generate report", type="primary"):
        try:
            with st.spinner("Reading 311 cases..."):
                report = generate_report(
                    choice["label"],
                    CASE_RADIUS_M,
                    f"{window[0]} to {window[1]}",
                    cases["categories"] if cases else {},
                    selected_cases,
                    parks,
                    stops["total"],
                    nearest_stops,
                )
            st.session_state["tenant_report"] = {"key": report_key, "report": report}
        except Exception:
            st.error(
                "Could not generate the report. Check the Mistral API key, "
                "model access, and network connection, then retry. "
                "The data above is still available."
            )

    saved = st.session_state.get("tenant_report")

    if not saved or saved["key"] != report_key:
        return

    report = saved["report"]
    evidence_by_id = {c["service_request_id"]: c for c in selected_cases}

    st.markdown("**Summary**")
    st.write(report["summary"])
    st.markdown("**Dog parks**")
    st.write(report["dog_parks"])
    st.markdown("**Muni stops**")
    st.write(report["transit"])

    if not report["findings"]:
        st.info(
            "The supplied records did not provide enough information "
            "for specific questions to ask."
        )

    for i, finding in enumerate(report["findings"], start=1):
        heading = finding.get("heading") or f"Finding {i}"
        st.markdown(f"**{heading}**")
        st.write(finding["observation"])
        st.write("Ask the landlord:", finding["landlord_question"])

        with st.expander(f"Supporting cases: {heading}"):
            for case_id in finding["case_ids"]:
                source = evidence_by_id[case_id]
                st.caption(
                    f"#{case_id} · {display_date(source.get('requested_datetime'))} · "
                    f"{source.get('service_name') or 'Unknown'} · "
                    f"{source.get('status') or 'Unknown'}"
                )
                st.text(
                    " ".join(
                        part
                        for part in (source.get("status_notes"), source.get("service_details"))
                        if part
                    )
                    or "No notes recorded."
                )

    st.caption(
        "AI-generated from public records. It can be wrong, and it cannot "
        "tell you the condition of this building. Verify with the landlord."
    )


st.title("Before You Rent")
st.write(
    "See the dog play areas, Muni stops, and 311 activity near an address "
    "before you sign a lease."
)

with st.form("address_search"):
    query = st.text_input(
        "Address you're thinking of renting",
        placeholder="Try 845 California",
    )
    submitted = st.form_submit_button("Search", type="primary")

if submitted:
    query = query.strip()
    if len(query) < 3:
        st.session_state["search"] = None
        st.warning("Enter at least 3 characters of a San Francisco address.")
        st.stop()

    try:
        st.session_state["search"] = {
            "query": query,
            "matches": search_addresses(query),
        }
    except Exception:
        st.session_state["search"] = None
        st.error("Could not search addresses. Please try again.")
        st.stop()

search = st.session_state.get("search")

if search is None:
    st.info("Enter a San Francisco street address and press Search.")
    st.stop()

matches = search["matches"]

if not matches:
    st.info(
        "No matching San Francisco address found. "
        "Start with the street number, then the street name "
        "without the suffix, like “845 California”."
    )
    st.stop()

if len(matches) == 1:
    choice = matches[0]
else:
    choice = st.selectbox(
        f"{len(matches)} addresses match — select one",
        options=matches,
        format_func=lambda m: m["label"],
        index=None,
        placeholder="Choose an address",
        key=f"choice-{search['query']}",
    )

if choice is None:
    st.stop()

st.header(choice["label"])

try:
    parks = get_dog_parks(choice["lat"], choice["lon"])
except Exception:
    parks = None
    st.error(
        "Could not retrieve dog parks. Check your Elasticsearch "
        "connection, API key, and index name."
    )

try:
    stops = get_stops(choice["lat"], choice["lon"])
except Exception:
    stops = None
    st.error(
        "Could not retrieve Muni stops. Check your Elasticsearch "
        "connection, API key, and index name."
    )

try:
    cases = get_cases(choice["lat"], choice["lon"])
    window = get_case_window()
except Exception:
    cases = None
    window = None
    st.error(
        "Could not retrieve 311 cases. Check your Elasticsearch "
        "connection, API key, and index name."
    )

if parks is not None and cases is not None and stops is not None:
    show_report(choice, cases, parks, stops, window)

if parks is not None:
    show_dog_parks(choice, parks)

if stops is not None:
    show_stops(choice, stops)

if cases is not None:
    show_cases(cases, window)
