import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# pi_client/ has no __init__.py (sensor_reader.py is a standalone script, not
# a package), so it isn't auto-discovered by pytest's rootdir import
# mechanism. Put it on sys.path explicitly so `from sensors.adc import ...`
# and `import sensor_reader` resolve the same way they do on the Pi.
PI_CLIENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PI_CLIENT_DIR))

# spidev is a Linux-only C extension bound to /dev/spidev - it doesn't
# install on Windows/most CI. Mocking it in sys.modules before any test
# imports sensors.adc lets that import succeed everywhere; individual tests
# control the mock's return values to test the MCP3008 bit math.
sys.modules.setdefault("spidev", MagicMock())

# sensor_reader.py reads these from os.environ at module import time.
# Real values don't matter for unit tests - individual tests override what
# they care about via monkeypatch.
os.environ.setdefault("DEVICE_ID", "test-device-id")
os.environ.setdefault("SENSOR_CONFIG", "adc:0:moisture")
os.environ.setdefault("PI_OUTBOX_DB", ":memory:")
os.environ.setdefault("MQTT_HOST", "test-broker")
os.environ.setdefault("MQTT_USERNAME", "test-user")
os.environ.setdefault("MQTT_PASSWORD", "test-pass")
os.environ.setdefault("MQTT_CA_FILE", "test-ca.crt")
