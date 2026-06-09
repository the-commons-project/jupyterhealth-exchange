"""Generate a synthetic demo cohort (CGM + Oura wearables) ending today.

Instead of loading recorded data, this synthesizes plausible CGM glucose and
Oura-style wearable observations for a fixed roster of demo patients. All data is
clearly synthetic (distinct acquisition_provenance.source_name) and dated up to
the run date.

Wearable simulation math references JP's student's generator
(github.com/dicristea/oura-clinical-workbench, demo_data/omh_ieee_generator).
"""

import uuid
from datetime import UTC, datetime

STUDY_NAME = "CGM & Wearables Demo"
ORG_NAME_FRAGMENT = "Planetary Research Institute"
CGM_DATA_SOURCE = "Dexcom"
WEARABLE_DATA_SOURCE = "Oura"
CGM_SOURCE_NAME = "demo-cgm-generator"
WEARABLE_SOURCE_NAME = "demo-synthea-omh-ieee-generator"  # credit: dicristea/oura-clinical-workbench
SEED = 4242

CGM_CODE = "omh:blood-glucose:4.0"
CGM_INTERVAL_MINUTES = 5
CGM_WINDOW_DAYS = 14  # recent dense CGM window (a realistic wear period)
WEARABLE_MIN_DAYS = 60  # per-patient wearable history is varied in this range...
WEARABLE_MAX_DAYS = 180  # ...so the cohort has a realistic spread, all ending today

OMH = "https://w3id.org/openmhealth"
IEEE = "https://w3id.org/ieee1752"

# scope coding_code -> (coding_system, label). Order is the per-day record order.
WEARABLE_SCOPES = [
    ("ieee:physical-activity:1.0", IEEE, "Physical activity"),
    ("ieee:sleep-episode:1.0", IEEE, "Sleep episode (IEEE)"),
    ("ieee:time-in-bed:1.0", IEEE, "Time in bed"),
    ("omh:heart-rate:2.0", OMH, "Heart Rate"),
    ("omh:respiratory-rate:2.0", OMH, "Respiratory rate"),
    ("omh:oxygen-saturation:2.0", OMH, "Oxygen saturation"),
    ("omh:total-sleep-time:1.0", OMH, "Total sleep time"),
    ("omh:sleep-episode:1.1", OMH, "Sleep episode"),
]

MOCK_PATIENTS = [
    {
        "name_family": "Nguyen",
        "name_given": "May",
        "birth_date": "1984-07-11",
        "telecom_phone": "265-642-0143",
        "email": "may.nguyen@example.com",
    },
    {
        "name_family": "Smith",
        "name_given": "Olivia",
        "birth_date": "1976-03-23",
        "telecom_phone": "187-554-0198",
        "email": "olivia.smith@example.com",
    },
    {
        "name_family": "Chen",
        "name_given": "Liang",
        "birth_date": "1948-11-30",
        "telecom_phone": "997-576-0102",
        "email": "liang.chen@example.com",
    },
    {
        "name_family": "Patel",
        "name_given": "Anika",
        "birth_date": "1989-01-17",
        "telecom_phone": "345-233-0170",
        "email": "anika.patel@example.com",
    },
    {
        "name_family": "Garcia",
        "name_given": "Carlos",
        "birth_date": "1955-05-04",
        "telecom_phone": "609-442-0186",
        "email": "carlos.garcia@example.com",
    },
    {
        "name_family": "Okafor",
        "name_given": "Chinelo",
        "birth_date": "1962-08-19",
        "telecom_phone": "435-287-0116",
        "email": "chinelo.okafor@example.com",
    },
    {
        "name_family": "Kowalski",
        "name_given": "Zofia",
        "birth_date": "1945-02-14",
        "telecom_phone": "399-765-0124",
        "email": "zofia.kowalski@example.com",
    },
    {
        "name_family": "Tanaka",
        "name_given": "Hiroshi",
        "birth_date": "1958-10-01",
        "telecom_phone": "298-443-0131",
        "email": "hiroshi.tanaka@example.com",
    },
    {
        "name_family": "Abdullah",
        "name_given": "Layla",
        "birth_date": "1973-12-25",
        "telecom_phone": "198-619-0149",
        "email": "layla.abdullah@example.com",
    },
    {
        "name_family": "Dubois",
        "name_given": "Emile",
        "birth_date": "1981-06-03",
        "telecom_phone": "400-870-0162",
        "email": "emile.dubois@example.com",
    },
    {
        "name_family": "Singh",
        "name_given": "Raj",
        "birth_date": "1992-09-12",
        "telecom_phone": "398-112-0181",
        "email": "raj.singh@example.com",
    },
    {
        "name_family": "Martinez",
        "name_given": "Sofia",
        "birth_date": "1967-07-27",
        "telecom_phone": "229-998-0108",
        "email": "sofia.martinez@example.com",
    },
    {
        "name_family": "Kim",
        "name_given": "Jisoo",
        "birth_date": "1950-04-20",
        "telecom_phone": "988-889-0157",
        "email": "jisoo.kim@example.com",
    },
    {
        "name_family": "Ivanov",
        "name_given": "Dmitri",
        "birth_date": "1983-02-05",
        "telecom_phone": "799-443-0129",
        "email": "dmitri.ivanov@example.com",
    },
    {
        "name_family": "Mbatha",
        "name_given": "Sipho",
        "birth_date": "1979-11-08",
        "telecom_phone": "762-112-0140",
        "email": "sipho.mbatha@example.com",
    },
    {
        "name_family": "Rossi",
        "name_given": "Giulia",
        "birth_date": "1960-05-30",
        "telecom_phone": "772-981-0169",
        "email": "giulia.rossi@example.com",
    },
    {
        "name_family": "Hernandez",
        "name_given": "Luis",
        "birth_date": "1952-03-14",
        "telecom_phone": "118-112-0194",
        "email": "luis.hernandez@example.com",
    },
    {
        "name_family": "Yilmaz",
        "name_given": "Aylin",
        "birth_date": "1972-01-01",
        "telecom_phone": "388-887-0175",
        "email": "aylin.yilmaz@example.com",
    },
    {
        "name_family": "Andersson",
        "name_given": "Lars",
        "birth_date": "1988-10-10",
        "telecom_phone": "334-874-0111",
        "email": "lars.andersson@example.com",
    },
    {
        "name_family": "Ali",
        "name_given": "Zara",
        "birth_date": "1947-06-06",
        "telecom_phone": "202-555-0188",
        "email": "zara.ali@example.com",
    },
]


def _iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def _uv(value, unit, digits=1):
    return {"value": round(float(value), digits), "unit": unit}


def _dur(seconds):
    return {"value": int(round(float(seconds))), "unit": "sec"}


def _interval(start, end):
    return {
        "time_interval": {
            "start_date_time": _iso(start),
            "end_date_time": _iso(end),
            "duration": _dur((end - start).total_seconds()),
        }
    }


def _wearable_header(coding_code):
    namespace, name, version = coding_code.split(":", 2)
    return {
        "header": {
            "uuid": str(uuid.uuid4()),
            "source_creation_date_time": _iso(datetime.now(UTC)),
            "schema_id": {"namespace": namespace, "name": name, "version": version},
            "modality": "sensed",
            "acquisition_provenance": {"source_name": WEARABLE_SOURCE_NAME},
        }
    }
