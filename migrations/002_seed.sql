-- Seed the real hardware: one Pi, six plants, six wired moisture sensors.
--
-- This is Phase 1.3 of the plan, deferred until now because nothing read these
-- rows until the Consumer existed. Without them every reading the Pi publishes
-- is dead-lettered as `unknown_sensor`.
--
-- Only what is physically wired is seeded. The tank-level sensor (adc:6), light
-- sensor (adc:7), DHT22 (gpio:4:temp / gpio:4:hum) and the relay module do not
-- exist yet, so their `sensors` rows would be lies.
--
-- `watering_config` is deliberately NOT seeded for the same reason: relay_channel
-- is NOT NULL and maps to a physical solenoid, none of which are wired. With no
-- row, auto_water short-circuits and no command can ever be created. Seed it (or
-- create it via the API) when the relays go in.

-- Plants are known to their owner by their Russian names, which stay in `name`
-- as the display value. `name_en` carries the common English name so the API,
-- logs and any future non-Russian UI have something readable to fall back on.
-- Nullable: a plant added later without a known English name is still valid.
ALTER TABLE plants ADD COLUMN name_en VARCHAR(100);

INSERT INTO devices (id, code, label)
VALUES ('260922ba-03f3-4c20-a7e4-1d7dbf8d9f42', 'klwater', 'Raspberry Pi (klwater)')
ON CONFLICT (id) DO NOTHING;

-- Plants and their sensors are inserted together: plants.id is GENERATED ALWAYS
-- AS IDENTITY, so the sensor rows can only learn their plant_id from RETURNING.
-- The join is on name, not on RETURNING order — that order is not guaranteed.
-- (The ORDER BY only makes the generated ids line up with channel order, which
-- is a readability nicety, not something correctness rests on.)
WITH wiring (channel, plant_name, plant_name_en) AS (
    VALUES
        (0, 'Кофейное дерево', 'Coffee tree'),
        (1, 'Пеперомия',       'Peperomia'),
        (2, 'Нефролепис',      'Boston fern'),
        (3, 'Алоказия',        'Alocasia'),
        (4, 'Спатифиллум',     'Peace lily'),
        (5, 'Монстера',        'Monstera')
),
new_plants AS (
    INSERT INTO plants (name, name_en)
    SELECT plant_name, plant_name_en FROM wiring ORDER BY channel
    RETURNING id, name
)
INSERT INTO sensors (device_id, sensor_type_id, plant_id, address, label)
SELECT
    '260922ba-03f3-4c20-a7e4-1d7dbf8d9f42'::uuid,
    (SELECT id FROM sensor_types WHERE code = 'moisture'),
    p.id,
    'adc:' || w.channel,
    p.name || ' moisture'
FROM new_plants p
JOIN wiring w ON w.plant_name = p.name;
