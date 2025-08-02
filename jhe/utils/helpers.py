import json
import random
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.utils import timezone


def generate_observation_value_attachment_data(coding_code):
    data_point = settings.DATA_DIR_PATH.data_point_dir / (coding_code.replace(":", "_").replace(".", "-") + ".json")
    if not data_point.exists():
        return "placeholder"

    placeholder = json.loads(data_point.read_text())

    placeholder.get("header")["uuid"] = str(uuid4())
    placeholder.get("header")["uuid"] = str(timezone.now())

    body = placeholder.get("body")
    for key in ("body_temperature", "oxygen_saturation", "respiratory_rate"):
        field = body.get(key)
        if field and "value" in field:
            field["value"] += random.randint(1, 10)

    body["effective_time_frame"] = {"date_time": str(timezone.now() + timedelta(hours=1))}
    return placeholder
