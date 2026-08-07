import os

# bot.config reads these at import time.
os.environ.setdefault("BOT_TOKEN", "123456:TEST-BOT-TOKEN")
os.environ.setdefault("OWNER_USER_ID", "424242")
os.environ.setdefault("SESSION_SECRET", "0" * 64)

OWNER_ID = 424242
STRANGER_ID = 999999


def plant(pid=1, name="Монстера", name_en="Monstera", location="Kitchen",
          mode="manual", auto_threshold=30.0, last_watered_at=None):
    return {"id": pid, "name": name, "name_en": name_en, "location": location,
            "created_at": "2026-08-07T09:00:00+00:00", "mode": mode,
            "auto_threshold": auto_threshold, "last_watered_at": last_watered_at}


def sensor(sensor_id=1, plant_id=1, sensor_type="moisture", unit="%", value=36.7,
           address="adc:5", label="Монстера moisture", recorded_at="2026-08-07T10:00:00+00:00"):
    return {"sensor_id": sensor_id, "plant_id": plant_id, "address": address,
            "label": label, "sensor_type": sensor_type, "unit": unit,
            "value": value, "raw_adc": 566, "recorded_at": recorded_at}
