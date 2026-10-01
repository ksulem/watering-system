"""The relay wiring, written down once.

Imported by both `watering_controller.py` (production) and `test_relay.py`
(bench). They used to carry separate copies of the pin map with *different
channel numbering* — the bench script numbered the six wired channels 1-6
sequentially while the database used the relay board's own IN labels — so the
same integer meant two different plants depending on which file you read.
Nothing detects that at runtime: every command succeeds, the logs stay clean,
and the wrong plant gets watered.

`migrations/tests/test_0003_relays.py` asserts RELAY_PINS below matches the
`relay_channel` values seeded into `watering_config`, so a rewire that updates
only one side fails the test suite instead of the houseplants.

No RPi.GPIO import here on purpose: this module is pure data so it imports on a
dev machine, and the level constants are plain ints, which RPi.GPIO accepts
anywhere it accepts GPIO.HIGH/GPIO.LOW.
"""

# Keys are the relay board's input labels (IN1..IN8) and match
# watering_config.relay_channel. 4 and 7 are absent because nothing drives them
# and no solenoid is attached — a command naming one must fail as
# `invalid_relay_channel` rather than silently doing nothing. Do NOT renumber to
# close the gap: these are the numbers silkscreened on the board, which is what
# you read when you are standing over it trying to work out what went wrong.
RELAY_PINS = {
    1: 5,    # IN1, header pin 29 — Кофейное дерево / Coffee tree   (adc:0)
    2: 6,    # IN2, header pin 31 — Пеперомия / Peperomia           (adc:1)
    3: 13,   # IN3, header pin 33 — Нефролепис / Boston fern        (adc:2)
    5: 19,   # IN5, header pin 35 — Алоказия / Alocasia             (adc:3)
    6: 26,   # IN6, header pin 37 — Спатифиллум / Peace lily        (adc:4)
    8: 21,   # IN8, header pin 40 — Монстера / Monstera             (adc:5)
}

# Display names only — the database is the source of truth for which plant a
# channel waters. Kept here so the bench script can name what just clicked.
CHANNEL_LABELS = {
    1: "Кофейное дерево / Coffee tree",
    2: "Пеперомия / Peperomia",
    3: "Нефролепис / Boston fern",
    5: "Алоказия / Alocasia",
    6: "Спатифиллум / Peace lily",
    8: "Монстера / Monstera",
}

# The wired board is active-LOW: pulling a pin LOW energises its relay. Every
# call site derives its levels from these two names instead of writing HIGH or
# LOW literally, so swapping to an active-high board is a one-line change and
# cannot leave a stray inverted literal behind driving a pump.
RELAY_ACTIVE_HIGH = False
ENERGISED = int(RELAY_ACTIVE_HIGH)         # 0 on this board
DE_ENERGISED = int(not RELAY_ACTIVE_HIGH)  # 1 on this board

# Boot state — MEASURED on this board, 2026-08-09, not assumed.
#
# The Pi's SoC boots GPIO0-8 with an internal pull-up and GPIO9-27 with an
# internal pull-down, which on an active-low board would leave channels 3/5/6/8
# (GPIO13/19/26/21) energised from power-on until software runs. That is the
# textbook hazard, and it does NOT apply here: this board pulls its own IN lines
# up, and that pull-up wins against the SoC's ~50k internal pull-down. Measured
# by forcing the boot condition and reading the level back:
#
#     $ for p in 13 19 21 26; do pinctrl set $p ip pd; done; pinctrl get 13,19,21,26
#     13: ip    pd | hi     19: ip    pd | hi
#     21: ip    pd | hi     26: ip    pd | hi
#
# hi with the pull-down active means de-energised at power-on. No `gpio=...=op,dh`
# line in /boot/firmware/config.txt is needed. The same measurement also explains
# why GPIO.cleanup() is safe: it leaves the pins as inputs, which float high here.
#
# This is a property of the BOARD, not of the Pi. Re-run the measurement above if
# the relay module is ever replaced — a board without that pull-up would sit with
# four pumps running from every power-on.
