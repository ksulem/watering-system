# CLAUDE.md

IoT plant-watering system: Raspberry Pi (sensors + relays) → MQTT/TLS → Oracle Cloud VPS
(TimescaleDB, Consumer, Command Dispatcher, FastAPI, Telegram bot, Nginx).

Read before working:
- [`docs/PROGRESS.md`](docs/PROGRESS.md) — what's done, what's open, uncommitted work, new-machine checklist
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — access, deploy procedures, gotchas
- [`docs/HARDWARE.md`](docs/HARDWARE.md) — Pi, sensors, relay wiring
- [`docs/AGENT-GUIDE.md`](docs/AGENT-GUIDE.md) — how to connect to the Pi, VPS, DB and MQTT, and what needs user approval
- [`plant-monitor-plan.md`](plant-monitor-plan.md) — full design (schema, MQTT contracts, phases)

Working rules:
- Never run or assume consent for Windows registry edits — ask in the moment.
- Always carry an English name column next to Russian plant names in SQL/config.
- Never print secrets; the services read the **VPS** `.env`, not the local one.
- The VPS is 1 GB RAM — weigh every new container against memory.
- Keep `docs/PROGRESS.md` updated when a phase or open item changes.
