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
| `OVERLAY_HOST`        | `127.0.0.1` | bind address(es); comma-separated to bind several (e.g. `127.0.0.1,10.66.0.1`) — never `0.0.0.0` (unauthenticated service) |
| `OVERLAY_PORT`        | `43594`     | TCP port                             |
| `OVERLAY_DATA_DIR`    | `~/.runelite/overlay-service/` (fallback `./data`) | persistence dir |
| `OVERLAY_BAR_TYPE`    | `AUTO`      | `AUTO`/`IRON`/`STEEL`/`MITHRIL`/`ADAMANTITE`/`RUNITE` |
| `OVERLAY_LOG`         | `INFO`      | log level                            |

## Exposing to a remote RuneLite

The service binds loopback only by default. Two ways to reach it from a RuneLite
on another host:

### WireGuard direct (preferred — no ssh forward in the hot path)

Put both machines on a WireGuard tunnel (here `10.66.0.1` = this host,
`10.66.0.2` = the RuneLite box). Bind the service on the WG interface too and
point the plugin straight at it:

```bash
OVERLAY_HOST=127.0.0.1,10.66.0.1 OVERLAY_PORT=43599 python -m service
```

Then in the Overlay Bridge plugin config set **Host = `10.66.0.1`**, **Port =
`43599`**. The plugin talks to the service directly over WireGuard — no ssh
forward, so the live overlay is unaffected by any ssh control-channel hiccup.
Binding the WG address (not `0.0.0.0`) keeps the service reachable only over the
tunnel. The `onConfigChanged` handler bounces the connection when host/port
change, so this needs no client restart.

### SSH remote forward (fallback)

```bash
# run ON the machine where RuneLite runs; forwards its localhost:43594 to ours
ssh -R 43594:localhost:43599 user@this-host
```

The plugin connects to `127.0.0.1:43594` on its own machine and the traffic
tunnels here. Note this rides the ssh channel in the hot path — a dropped tunnel
drops the overlay; prefer the WireGuard direct route above.

## Test

```bash
pip install -e '.[test]'   # or: pip install pytest
pytest -q
```

Covers the policy port (coal:ore ratios, the coal-before-ore gate, the
adamantite `fcoal < 54` trip split, the coal+ore **both**-highlight fix, coffer
priority, belt/return-leg sequencing) and a live `hello -> subscribe -> state ->
render` socket round-trip.
