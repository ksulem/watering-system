-- Seed `watering_config` for the six plants now that the relay module is wired.
--
-- 002_seed.sql deliberately left this table empty: relay_channel is NOT NULL and
-- names a physical solenoid, and none existed. They exist now, so the rows are
-- no longer lies.
--
-- The channel numbers are NOT contiguous, and that is deliberate — they record
-- the physical wiring, which skips IN4 and IN7:
--
--   plant              (en)          moisture   relay_channel   relay IN   GPIO (BCM)   header pin
--   Кофейное дерево    Coffee tree     adc:0           1           IN1          5            29
--   Пеперомия          Peperomia       adc:1           2           IN2          6            31
--   Нефролепис         Boston fern     adc:2           3           IN3         13            33
--   Алоказия           Alocasia        adc:3           5           IN5         19            35
--   Спатифиллум        Peace lily      adc:4           6           IN6         26            37
--   Монстера           Monstera        adc:5           8           IN8         21            40
--
-- Channels 4 and 7 have no GPIO driving them and no solenoid attached. They must
-- stay absent from the Pi's RELAY_PINS so a command naming one fails fast as
-- `invalid_relay_channel` rather than silently doing nothing.
--
-- Because the channels have gaps, relay_channel is NOT derivable from the ADC
-- channel. This table is the only source of truth for the pairing; RELAY_PINS on
-- the Pi maps channel->GPIO and must agree with the table above.

-- As of this migration ONLY the relay board is connected — no pumps, solenoids,
-- tubing or tank. Firing a channel clicks a relay and moves no water. That makes
-- the whole command path (API -> outbox -> dispatcher -> MQTT -> Pi -> event ->
-- Consumer) safe to exercise end to end right now, which is the point of seeding
-- these rows before the plumbing exists.
--
-- mode is 'manual', overriding the schema default of 'auto', and it matters most
-- *later*: if these rows were 'auto', auto_water would begin firing the moment
-- pumps are attached, on whatever moisture readings happened to be current, with
-- nobody watching. Manual now means plumbing day is deliberate — flip each plant
-- to 'auto' via PATCH /plants/{id}/watering/config after its line has been
-- watched end to end.
--
-- duration_sec is left at the 10s default. It is a placeholder until there is a
-- pump whose flow rate can be measured — tune per plant then.
-- plant_name_en is carried alongside but never read: the join is on `name`, the
-- display value. It is here so this file is legible without Russian, and it
-- matches plants.name_en set by 002_seed.sql — if one is edited, edit both.
WITH relay_wiring (plant_name, plant_name_en, relay_channel) AS (
    VALUES
        ('Кофейное дерево', 'Coffee tree', 1),
        ('Пеперомия',       'Peperomia',   2),
        ('Нефролепис',      'Boston fern', 3),
        ('Алоказия',        'Alocasia',    5),
        ('Спатифиллум',     'Peace lily',  6),
        ('Монстера',        'Monstera',    8)
)
INSERT INTO watering_config (plant_id, device_id, mode, relay_channel)
SELECT
    p.id,
    '260922ba-03f3-4c20-a7e4-1d7dbf8d9f42'::uuid,
    'manual',
    w.relay_channel
FROM relay_wiring w
JOIN plants p ON p.name = w.plant_name
ON CONFLICT (plant_id) DO NOTHING;
