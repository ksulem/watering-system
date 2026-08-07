"""Rendering helpers for bot messages.

Kept separate from the handlers so the message layout can be unit-tested
without an aiogram Message or a live API.
"""
from datetime import UTC, datetime

SENSOR_ICON = {
    "moisture": "💧",
    "temperature": "🌡",
    "humidity": "💦",
    "water_level": "🪣",
    "light": "☀️",
}

# Fallback when a plant has no watering_config to supply auto_threshold. Only
# drives the ⚠️ marker in /status — nothing waters on the strength of it.
DEFAULT_LOW_MOISTURE = 30.0


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def time_ago(value: str | None, *, now: datetime | None = None) -> str:
    dt = parse_dt(value)
    if dt is None:
        return "never"
    delta = (now or datetime.now(UTC)) - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def plant_title(plant: dict) -> str:
    """Russian name first — that's what the owner calls the plant."""
    name = plant.get("name") or "Unnamed"
    name_en = plant.get("name_en")
    title = f"{name} ({name_en})" if name_en and name_en != name else name
    location = plant.get("location")
    return f"{title} — {location}" if location else title


def format_reading(sensor: dict, low_threshold: float | None) -> str:
    icon = SENSOR_ICON.get(sensor["sensor_type"], "•")
    label = sensor["sensor_type"].replace("_", " ").capitalize()
    value = sensor.get("value")
    if value is None:
        return f"   {icon} {label}: no reading yet"

    unit = sensor.get("unit") or ""
    # sensor_types.unit is '%' / '°C' / 'lux' — space only before word-ish units.
    sep = "" if unit in ("%", "°C", "%RH") else " "
    text = f"   {icon} {label}: {value:.0f}{sep}{unit}"

    if sensor["sensor_type"] == "moisture":
        limit = low_threshold if low_threshold is not None else DEFAULT_LOW_MOISTURE
        if value < limit:
            text += " ⚠️ Low"
        else:
            text += " ✅"
    return text


def format_status(plants: list[dict], sensors: list[dict],
                  *, now: datetime | None = None) -> str:
    by_plant: dict[int, list[dict]] = {}
    device_level: list[dict] = []
    for s in sensors:
        if s.get("plant_id") is None:
            device_level.append(s)
        else:
            by_plant.setdefault(s["plant_id"], []).append(s)

    lines = ["🌿 <b>Plant Status</b>", ""]
    if not plants:
        lines.append("No plants registered yet.")
        return "\n".join(lines)

    for plant in plants:
        lines.append(f"🪴 <b>{plant_title(plant)}</b>")
        readings = by_plant.get(plant["id"], [])
        if not readings:
            lines.append("   (no sensors attached)")
        for sensor in readings:
            lines.append(format_reading(sensor, plant.get("auto_threshold")))

        mode = plant.get("mode")
        if mode:
            lines.append(f"   🔄 Mode: {mode.upper()}")
            lines.append(f"   💦 Last watered: {time_ago(plant.get('last_watered_at'), now=now)}")
        else:
            # No watering_config row: no relay is plumbed to this plant yet.
            lines.append("   🔧 No relay assigned")
        lines.append("")

    if device_level:
        lines.append("<b>Device sensors</b>")
        for sensor in device_level:
            lines.append(format_reading(sensor, None))

    return "\n".join(lines).strip()


def format_water_targets(plants: list[dict]) -> str:
    """Message shown when /water is sent without a plant id."""
    waterable = [p for p in plants if p.get("mode") == "manual"]
    if not waterable:
        configured = [p for p in plants if p.get("mode")]
        if not configured:
            return ("🔧 No plant has a relay assigned yet, so there's nothing to "
                    "water.\n\nAssign one with "
                    "<code>PUT /plants/{id}/watering/config</code> once the relay "
                    "hardware is wired up.")
        return ("All configured plants are in AUTO mode. Switch one to MANUAL "
                "before watering by hand — otherwise a manual command would race "
                "the automatic threshold logic.")
    lines = ["💦 <b>Which plant?</b>", ""]
    for p in waterable:
        lines.append(f"   /water_{p['id']} — {plant_title(p)}")
    return "\n".join(lines)
