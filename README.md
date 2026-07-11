# osrs-overlay-service

The **logic half** of a thin-client architecture for a RuneLite overlay plugin.
The plugin is a dumb, generic renderer; **this service holds all domain logic**
(the tuned Blast Furnace policy, coal math, trip computer, hotspots, action log).

It speaks the newline-delimited JSON bridge protocol — see
[`PROTOCOL.md`](https://github.com/TechDevGroup/) (the authoritative contract this
implements): the service is the **TCP server**, the plugin is the client.

- Restart the service freely — the RuneLite client stays up and auto-reconnects.
- Edit `service/policy_bf.py` and save — the service **hot-reloads** the policy
  module (`importlib.reload`) without dropping the socket. Game logic iterates
  with no RuneLite restart.

## Layout

```
service/
  server.py       asyncio TCP server, protocol handling, hot-reload watcher
  policy_bf.py    Blast Furnace policy (pure port of BFPolicy.java) + HUD +
                  directive assembly + raw-state -> snapshot adapter
  state_store.py  persistence: hotspots.json, bank-layout.json, actions.log +
                  session accounting (coal-bag count, deposits, bars, runtime)
  ids.py          item / object / varbit ids (ported from BFConstants.java)
tests/            pytest: policy port + a mock-client TCP round-trip
```

The policy is ported 1:1 from
[`TechDevGroup/runelite-blast-furnace-helper`](https://github.com/TechDevGroup/runelite-blast-furnace-helper)
(`BFPolicy.java`, `BFStateSnapshot.java`, `BFConstants.java`, `BarType.java`).

## Run

```bash
python -m service
```

Listens on `127.0.0.1:43594` by default. Environment overrides:

| var                   | default     | meaning                              |
|-----------------------|-------------|--------------------------------------|
| `OVERLAY_HOST`        | `127.0.0.1` | bind address (keep it loopback)      |
| `OVERLAY_PORT`        | `43594`     | TCP port                             |
| `OVERLAY_DATA_DIR`    | `~/.runelite/overlay-service/` (fallback `./data`) | persistence dir |
| `OVERLAY_BAR_TYPE`    | `AUTO`      | `AUTO`/`IRON`/`STEEL`/`MITHRIL`/`ADAMANTITE`/`RUNITE` |
| `OVERLAY_LOG`         | `INFO`      | log level                            |

## Exposing to a remote RuneLite over SSH

The service binds loopback only. To reach it from a RuneLite running on another
host, use an SSH **remote forward** from the RuneLite machine back to this host:

```bash
# run ON the machine where RuneLite runs; forwards its localhost:43594 to ours
ssh -R 43594:localhost:43594 user@this-host
```

The plugin then connects to `127.0.0.1:43594` on its own machine and the traffic
tunnels here. Loopback-only + SSH transport keeps the trust boundary intact.

## Test

```bash
pip install -e '.[test]'   # or: pip install pytest
pytest -q
```

Covers the policy port (coal:ore ratios, the coal-before-ore gate, the
adamantite `fcoal < 54` trip split, the coal+ore **both**-highlight fix, coffer
priority, belt/return-leg sequencing) and a live `hello -> subscribe -> state ->
render` socket round-trip.
