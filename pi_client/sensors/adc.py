import spidev

from .base import Sensor

_spi = spidev.SpiDev()
_spi.open(0, 0)
_spi.max_speed_hz = 1350000

# Throw away one conversion. The first xfer2 after a fresh open() reliably comes
# back as ~0 no matter what the sensor reads — measured on this Pi as
# [0, 636, 637, 636, 636] for five back-to-back reads of one channel.
#
# The legacy monitor.py never hit this: it opens the bus once and then reads for
# months, so only its very first logged row was affected. sensor_reader.py is a
# 60-second one-shot, so without this discard EVERY run records whichever
# channel it reads first as ~0 — which calibrates to a fabricated 100% moisture
# and would also stop auto-water from ever firing for that plant.
_spi.xfer2([1, 8 << 4, 0])


def read_mcp3008(channel: int) -> int:
    if channel < 0 or channel > 7:
        raise ValueError(f"MCP3008 channel must be 0-7, got {channel}")
    adc = _spi.xfer2([1, (8 + channel) << 4, 0])
    return ((adc[1] & 3) << 8) + adc[2]


class ADCSensor(Sensor):
    """Pi is a transducer — it publishes raw_adc only. The Consumer applies
    per-sensor calibration server-side so recalibration never requires a Pi
    deploy or a Pi -> API auth channel."""

    def __init__(self, channel: int, sensor_type: str):
        super().__init__(f"adc:{channel}", sensor_type)
        self.channel = channel

    async def read(self) -> list[dict]:
        raw = read_mcp3008(self.channel)
        return [{"address": self.address, "value": None,
                 "raw_adc": raw, "sensor_type": self.sensor_type}]
