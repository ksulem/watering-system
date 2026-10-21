# Hardware

## Raspberry Pi

- **Raspberry Pi 4 Model B**, aarch64, hostname `klwater` (`klwater.local` via mDNS),
  Raspberry Pi OS / Debian 13 "trixie", kernel 6.12.
- User `kwhatever`; password is in the repo-root `.env` (`PI_PASSWORD`), not here.
- Device ID: `260922ba-03f3-4c20-a7e4-1d7dbf8d9f42` (in `pi_client/.env` and the `devices`
  table). Never regenerate.
- Code lives at `~/plant-monitor/pi_client/` with its own `venv`.

### Services on the Pi

| Unit | Role |
|---|---|
| `plant-sensor.timer` / `.service` | One-shot `sensor_reader.py` every ~70s (60s + systemd `AccuracySec`). |
| `watering-controller.service` | Always-on; relay GPIO, MQTT commands, owns the `status` topic + last-will. `Type=notify`, `WatchdogSec=60`. |
| `watering-monitor.service` | **Legacy, stopped + disabled.** Holds SPI open forever — re-enabling it starves `plant-sensor`. |

### Pi gotchas

- **`RPi.GPIO` is the `python3-rpi-lgpio` apt shim**, not real RPi.GPIO. `pip install RPi.GPIO`
  and `pip install rpi-lgpio` both fail (no swig). The venv reaches the apt copy via
  `echo /usr/lib/python3/dist-packages > venv/lib/python3.13/site-packages/system-dist-packages.pth`.
- **SPI warm-up:** the first `xfer2` after `SpiDev.open()` returns ~0. `sensors/adc.py` discards
  one conversion at import. Any new module that opens its own bus needs the same.
- Manual runs: stop `plant-sensor.timer` first, then `set -a && . ./.env && set +a`.
- Requires `chrony` NTP-synced; `sensor_reader.py` refuses to publish otherwise.

## Sensors

Address scheme (`sensors.address`): `adc:N` (MCP3008), `gpio:4:temp` / `gpio:4:hum` (DHT22,
two logical sensors from one read), future `i2c:0x77:pressure`. Pi sensor list is driven by
the `SENSOR_CONFIG` env var.

**Actually wired:** only the 6 capacitive moisture sensors on ADC 0–5.
**Planned, not wired:** `adc:6` tank water level, `adc:7` light, DHT22 on GPIO4.

## Relay board — wired 2026-08-09

8-channel, **active LOW** (energises on `GPIO.LOW`). Only the board is connected — no pumps yet.

| Plant (ru / en) | Moisture | `relay_channel` | Relay IN | GPIO (BCM) | Header pin |
|---|---|---|---|---|---|
| Кофейное дерево / Coffee tree | adc:0 | 1 | IN1 | 5 | 29 |
| Пеперомия / Peperomia | adc:1 | 2 | IN2 | 6 | 31 |
| Нефролепис / Boston fern | adc:2 | 3 | IN3 | 13 | 33 |
| Алоказия / Alocasia | adc:3 | 5 | IN5 | 19 | 35 |
| Спатифиллум / Peace lily | adc:4 | 6 | IN6 | 26 | 37 |
| Монстера / Monstera | adc:5 | 8 | IN8 | 21 | 40 |

- `relay_channel` is the **silkscreen label**, not an index. 4 and 7 are unwired and absent.
  `watering_config` is the only source of truth for plant↔relay pairing.
- `pi_client/relay.py` `RELAY_PINS` is the single source of truth for channel→GPIO;
  `migrations/tests/test_0003_relays.py` fails if it drifts from the seed.
- **Boot-state hazard ruled out by measurement — don't "fix" it.** The board's own pull-ups
  beat the SoC's pull-downs on GPIO13/19/21/26, so relays stay off at boot and after
  `GPIO.cleanup()`. No `config.txt` `gpio=` line needed. Re-measure if the board is replaced:
  `for p in 13 19 21 26; do pinctrl set $p ip pd; done; pinctrl get 13,19,21,26` → all `hi`.
