import asyncio

import adafruit_dht
import board

from .base import Sensor

DHT22_READ_TIMEOUT_SEC = 3.0   # adafruit_dht can block on bad timing; cap it.


class DHT22Sensor(Sensor):
    def __init__(self, gpio_pin: int):
        # Base address is the physical attachment point ("gpio:4"), used for
        # logging only; read() emits per-value sub-addresses ("gpio:4:temp",
        # "gpio:4:hum") that match the sensors table.
        super().__init__(f"gpio:{gpio_pin}", "dht22")
        self._dht = adafruit_dht.DHT22(getattr(board, f"D{gpio_pin}"))
        self._gpio_pin = gpio_pin

    def _blocking_read(self) -> tuple[float | None, float | None]:
        # Property access is what actually triggers the GPIO read and may block.
        return self._dht.temperature, self._dht.humidity

    async def read(self) -> list[dict]:
        # Run the sync library in an executor with a hard timeout so a wedged
        # GPIO read can't stall the entire sensor cycle. On timeout, raise so
        # sensor_reader's per-sensor try/except logs it and moves on.
        loop = asyncio.get_running_loop()
        temp, hum = await asyncio.wait_for(
            loop.run_in_executor(None, self._blocking_read),
            timeout=DHT22_READ_TIMEOUT_SEC,
        )
        base = f"gpio:{self._gpio_pin}"
        return [
            {"address": f"{base}:temp", "value": temp,
             "raw_adc": None, "sensor_type": "temperature"},
            {"address": f"{base}:hum", "value": hum,
             "raw_adc": None, "sensor_type": "humidity"},
        ]
