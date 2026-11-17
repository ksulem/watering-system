#!/usr/bin/env python3
"""Тест релейного модуля — по очереди щёлкает каждым каналом.

Bench tool, not a pytest file (pytest only collects pi_client/tests/). Run it on
the Pi by hand after any rewiring:

    ~/plant-monitor/pi_client/venv/bin/python3 test_relay.py        # all channels
    ~/plant-monitor/pi_client/venv/bin/python3 test_relay.py 5      # just IN5

Channel numbers are the board's IN labels and come from relay.py, so they mean
the same thing here, in watering_controller.py and in the database. The earlier
version of this script numbered the same six channels 1-6 sequentially, which
made its channel 4 mean the plant the database calls channel 5.
"""
import sys
import time

import RPi.GPIO as GPIO
from relay import CHANNEL_LABELS, DE_ENERGISED, ENERGISED, RELAY_PINS

HOLD_SEC = 1.0
GAP_SEC = 0.5


def main(argv: list[str]) -> int:
    if argv:
        try:
            channels = [int(a) for a in argv]
        except ValueError:
            print(f"Каналы должны быть числами, получено: {' '.join(argv)}")
            return 2
        unknown = [c for c in channels if c not in RELAY_PINS]
        if unknown:
            print(f"Неизвестные каналы: {unknown}. Доступны: {sorted(RELAY_PINS)}")
            return 2
    else:
        channels = sorted(RELAY_PINS)

    GPIO.setmode(GPIO.BCM)
    # initial= sets the level as part of setup. A bare setup() followed by
    # output() would leave the pin at its default LOW — energised on this
    # board — for the moment in between, which is audible as a click and is a
    # real pump pulse.
    for pin in RELAY_PINS.values():
        GPIO.setup(pin, GPIO.OUT, initial=DE_ENERGISED)

    print(f"Тест реле: каждое включится на {HOLD_SEC:g} с.")
    print("Слушай щелчки и смотри на светодиоды модуля!\n")

    try:
        for channel in channels:
            pin = RELAY_PINS[channel]
            label = CHANNEL_LABELS.get(channel, "?")
            print(f"Реле {channel} (IN{channel}, GPIO{pin}) — {label}: ВКЛ")
            GPIO.output(pin, ENERGISED)
            time.sleep(HOLD_SEC)
            GPIO.output(pin, DE_ENERGISED)
            print(f"Реле {channel}: ВЫКЛ\n")
            time.sleep(GAP_SEC)
        print("✅ Тест завершён!")
        return 0
    finally:
        # Runs on Ctrl-C and on exceptions too — a script that dies holding a
        # pump on empties the tank onto the floor.
        for pin in RELAY_PINS.values():
            GPIO.output(pin, DE_ENERGISED)
        GPIO.cleanup()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
