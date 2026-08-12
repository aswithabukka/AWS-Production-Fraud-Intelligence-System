"""Static reference data for the synthetic transaction world.

Kept separate from the generator so tests can build a small deterministic world
without importing the CLI.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Merchant category codes with rough real-world share of card transaction *count*.
# Weights are relative, not percentages — they get normalised at sampling time.
# Grocery/restaurant dominate by count; travel and jewelry are rare but high-value,
# which is what makes the amount z-score feature in silver interesting.
MCC_CATALOG: list[tuple[str, str, float, float, float]] = [
    # (mcc, description,               weight, amount_mu, amount_sigma)   lognormal params
    ("5411", "grocery_stores", 22.0, 3.6, 0.7),
    ("5812", "eating_places_restaurants", 18.0, 3.981, 0.75),
    ("5541", "service_stations_fuel", 11.0, 3.7, 0.55),
    ("5912", "drug_stores_pharmacies", 7.0, 3.2, 0.8),
    ("5311", "department_stores", 6.0, 4.1, 0.9),
    ("5999", "misc_specialty_retail", 6.0, 3.8, 1.0),
    ("4121", "taxicabs_rideshare", 5.0, 2.95, 0.6),
    ("5814", "fast_food_restaurants", 5.0, 2.5, 0.55),
    ("5732", "electronics_stores", 3.5, 5.2, 1.0),
    ("5942", "book_stores", 2.5, 3.3, 0.7),
    ("7011", "lodging_hotels", 2.5, 5.6, 0.8),
    ("4511", "airlines_air_carriers", 2.0, 6.0, 0.8),
    ("5944", "jewelry_stores", 1.2, 6.1, 1.1),
    ("7995", "betting_casino_gambling", 1.0, 4.8, 1.2),
    ("6011", "atm_cash_disbursement", 4.0, 5.0, 0.5),
    ("5967", "direct_marketing_inbound", 1.5, 4.0, 1.1),
    ("7372", "software_subscriptions", 1.8, 2.9, 0.7),
]

# MCCs that carry structurally higher fraud risk. Used to bias which merchant a
# fraudulent transaction targets — real card fraud is not uniform across categories.
HIGH_RISK_MCCS = frozenset({"5944", "7995", "5967", "5732", "6011", "4511"})

CHANNELS: list[tuple[str, float]] = [
    ("card_present", 52.0),
    ("ecommerce", 33.0),
    ("contactless", 11.0),
    ("recurring", 4.0),
]

# Channel mix for fraudulent transactions — card-not-present is over-represented.
FRAUD_CHANNELS: list[tuple[str, float]] = [
    ("ecommerce", 68.0),
    ("card_present", 14.0),
    ("contactless", 12.0),
    ("recurring", 6.0),
]

# (city, lat, lon, population weight) — the metro areas customers and merchants live in.
CITIES: list[tuple[str, float, float, float]] = [
    ("new_york", 40.7128, -74.0060, 18.0),
    ("los_angeles", 34.0522, -118.2437, 13.0),
    ("chicago", 41.8781, -87.6298, 9.0),
    ("dallas", 32.7767, -96.7970, 7.5),
    ("houston", 29.7604, -95.3698, 7.0),
    ("phoenix", 33.4484, -112.0740, 5.0),
    ("philadelphia", 39.9526, -75.1652, 6.0),
    ("san_antonio", 29.4241, -98.4936, 2.5),
    ("san_diego", 32.7157, -117.1611, 3.3),
    ("san_jose", 37.3382, -121.8863, 2.0),
    ("austin", 30.2672, -97.7431, 2.3),
    ("seattle", 47.6062, -122.3321, 4.0),
    ("denver", 39.7392, -104.9903, 2.9),
    ("boston", 42.3601, -71.0589, 4.9),
    ("atlanta", 33.7490, -84.3880, 6.1),
    ("miami", 25.7617, -80.1918, 6.2),
]

# Cities used as the far end of an impossible-geography pair. Deliberately far from
# every CITIES entry so the geo-distance feature in silver has an unambiguous signal.
OFFSHORE_CITIES: list[tuple[str, float, float]] = [
    ("lagos", 6.5244, 3.3792),
    ("moscow", 55.7558, 37.6173),
    ("jakarta", -6.2088, 106.8456),
    ("sao_paulo", -23.5505, -46.6333),
    ("bucharest", 44.4268, 26.1025),
    ("shenzhen", 22.5431, 114.0579),
]

CURRENCIES: list[tuple[str, float]] = [("USD", 97.0), ("CAD", 1.6), ("EUR", 1.0), ("GBP", 0.4)]

EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True)
class Merchant:
    merchant_id: str
    mcc: str
    mcc_description: str
    name: str
    city: str
    lat: float
    lon: float
    risk_tier: str  # low | medium | high


@dataclass(frozen=True)
class Customer:
    customer_id: str
    home_city: str
    home_lat: float
    home_lon: float
    primary_device_id: str
    # Multiplier on the category's typical amount — some customers simply spend more.
    spend_factor: float


def weighted_choice(rng: random.Random, items: list[tuple], weight_index: int = -1):
    """Pick one item from a list of tuples using the weight at `weight_index`."""
    weights = [item[weight_index] for item in items]
    return rng.choices(items, weights=weights, k=1)[0]


def jitter_location(rng: random.Random, lat: float, lon: float, radius_km: float) -> tuple[float, float]:
    """Scatter a point uniformly within `radius_km` of (lat, lon)."""
    # sqrt keeps the sample uniform over the disc rather than clustered at the centre.
    distance = radius_km * math.sqrt(rng.random())
    bearing = rng.uniform(0, 2 * math.pi)
    d_lat = (distance / EARTH_RADIUS_KM) * math.cos(bearing)
    d_lon = (distance / EARTH_RADIUS_KM) * math.sin(bearing) / math.cos(math.radians(lat))
    return (
        round(lat + math.degrees(d_lat), 6),
        round(lon + math.degrees(d_lon), 6),
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Mirrored by the silver-layer geo feature."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = p2 - p1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def build_merchants(rng: random.Random, count: int) -> list[Merchant]:
    """Build a stable merchant dimension. Referenced by the silver SCD join and the
    bronze referential-integrity data-quality rule."""
    merchants: list[Merchant] = []
    for i in range(count):
        mcc, description, _, _, _ = weighted_choice(rng, MCC_CATALOG, weight_index=2)
        city, lat, lon, _ = weighted_choice(rng, CITIES)
        m_lat, m_lon = jitter_location(rng, lat, lon, radius_km=25)
        if mcc in HIGH_RISK_MCCS:
            risk_tier = rng.choices(["low", "medium", "high"], weights=[20, 45, 35])[0]
        else:
            risk_tier = rng.choices(["low", "medium", "high"], weights=[70, 26, 4])[0]
        merchants.append(
            Merchant(
                merchant_id=f"MER{i:06d}",
                mcc=mcc,
                mcc_description=description,
                name=f"{description.replace('_', ' ').title()} #{i:05d}",
                city=city,
                lat=m_lat,
                lon=m_lon,
                risk_tier=risk_tier,
            )
        )
    return merchants


def build_customers(rng: random.Random, count: int) -> list[Customer]:
    customers: list[Customer] = []
    for i in range(count):
        city, lat, lon, _ = weighted_choice(rng, CITIES)
        c_lat, c_lon = jitter_location(rng, lat, lon, radius_km=30)
        customers.append(
            Customer(
                customer_id=f"CUST{i:07d}",
                home_city=city,
                home_lat=c_lat,
                home_lon=c_lon,
                primary_device_id=f"DEV-{rng.getrandbits(48):012x}",
                # Lognormal-ish spread: most customers near 1.0, a long tail of big spenders.
                spend_factor=round(math.exp(rng.gauss(0, 0.35)), 3),
            )
        )
    return customers
