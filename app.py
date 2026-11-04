import os

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from elasticsearch import Elasticsearch

load_dotenv()

INDEX = "before-you-rent-dogparks"
RADIUS_M = 1000
METERS_PER_MILE = 1609.344
ADDRESS_URL = "https://data.sf.gov/resource/3mea-di5p.json"
SOURCE_URL = "https://data.sf.gov/d/38b7-6db3"

st.set_page_config(
    page_title="Before You Rent",
    page_icon="🐕",
    layout="centered",
)


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
    return f"{rows[0]['address'].title()}, San Francisco {rows[0].get('zip_code', '')}".strip()


@st.cache_data(ttl=300)
def get_dog_parks(lat, lon):
    result = get_client().search(
        index=INDEX,
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


def miles(meters):
    return meters / METERS_PER_MILE


st.title("Before You Rent")
st.write(
    "Find dog play areas near an address "
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

try:
    parks = get_dog_parks(choice["lat"], choice["lon"])
except Exception:
    st.error(
        "Could not retrieve dog parks. Check your Elasticsearch "
        "connection, API key, and index name."
    )
    st.stop()

st.subheader(choice["label"])
st.metric(
    f"Dog play areas within {miles(RADIUS_M):.1f} miles ({RADIUS_M / 1000:g} km)",
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
    st.stop()

points = [{"lat": choice["lat"], "lon": choice["lon"], "color": "#d62728"}]
points += [
    {"lat": p["location"]["lat"], "lon": p["location"]["lon"], "color": "#2ca02c"}
    for p in parks
]
st.map(pd.DataFrame(points), latitude="lat", longitude="lon", color="color")
st.caption("Red: the address. Green: dog play areas.")

st.subheader("Nearest first")

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
            f"from {choice['label'].split(',')[0].title()}"
        )
        st.link_button(
            "Open in Google Maps",
            f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
        )

st.link_button("Open the original DataSF dataset", SOURCE_URL)
