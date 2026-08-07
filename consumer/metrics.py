"""In-process Prometheus metrics, exposed at GET /metrics on the health server.

No Prometheus container — scraping is external (Grafana Cloud free tier) or
manual. On a 1 GB VM this is the cheapest way to get a real signal source
instead of grepping logs.
"""
from prometheus_client import Counter, Gauge, Histogram

# Message processing
mqtt_messages_total = Counter(
    "mqtt_messages_total", "Messages received", ["topic_suffix"])
mqtt_dead_letters_total = Counter(
    "mqtt_dead_letters_total", "Malformed messages", ["error_kind"])
mqtt_unknown_sensor_total = Counter(
    "mqtt_unknown_sensor_total",
    "Well-formed readings for unregistered (device, address, type) tuples",
    ["device_id"])

# DB
db_insert_seconds = Histogram("db_insert_seconds", "Reading insert latency")

# Outbox
command_outbox_pending = Gauge(
    "command_outbox_pending", "Rows in command_outbox with status=pending")

# Commands
watering_commands_total = Counter(
    "watering_commands_total", "Watering commands by terminal status",
    ["status"])  # completed | failed | expired

# Alerts
alerts_sent_total = Counter(
    "alerts_sent_total", "Threshold alerts raised", ["alert_type"])

# Sensor health. A rising count here means wiring, not weather — the reading was
# outside anything a working sensor produces, so it was discarded rather than
# stored as a false 100%.
sensor_implausible_total = Counter(
    "sensor_implausible_total", "Readings rejected as physically implausible",
    ["address"])
