from abc import ABC, abstractmethod


class Sensor(ABC):
    def __init__(self, address: str, sensor_type: str):
        self.address = address
        self.sensor_type = sensor_type

    @abstractmethod
    async def read(self) -> list[dict]:
        """Returns a list of reading dicts (one per value). Each dict carries
        its own `address` — multi-value sensors (DHT22) emit sub-addresses of
        the sensor's base address (e.g. "gpio:4" -> "gpio:4:temp")."""
