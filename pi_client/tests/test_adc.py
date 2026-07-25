import pytest
from sensors import adc


@pytest.mark.parametrize(
    ("raw_bytes", "expected"),
    [
        ([0, 0, 0], 0),          # minimum
        ([0, 3, 255], 1023),     # maximum (10-bit)
        ([0, 1, 44], 300),
        ([0, 0, 128], 128),
    ],
)
def test_read_mcp3008_bit_math(monkeypatch, raw_bytes, expected):
    monkeypatch.setattr(adc._spi, "xfer2", lambda cmd: raw_bytes)
    assert adc.read_mcp3008(0) == expected


@pytest.mark.parametrize("channel", [-1, 8, 100])
def test_read_mcp3008_rejects_invalid_channel(channel):
    with pytest.raises(ValueError):
        adc.read_mcp3008(channel)


@pytest.mark.parametrize("channel", range(8))
def test_read_mcp3008_command_bytes_encode_channel(monkeypatch, channel):
    # MCP3008 single-ended read command: [start bit, (8 + channel) << 4, 0]
    sent = {}

    def fake_xfer2(cmd):
        sent["cmd"] = cmd
        return [0, 0, 0]

    monkeypatch.setattr(adc._spi, "xfer2", fake_xfer2)
    adc.read_mcp3008(channel)
    assert sent["cmd"] == [1, (8 + channel) << 4, 0]


async def test_adc_sensor_read_shape(monkeypatch):
    monkeypatch.setattr(adc._spi, "xfer2", lambda cmd: [0, 1, 44])
    sensor = adc.ADCSensor(channel=3, sensor_type="moisture")

    readings = await sensor.read()

    assert readings == [{
        "address": "adc:3",
        "value": None,
        "raw_adc": 300,
        "sensor_type": "moisture",
    }]
