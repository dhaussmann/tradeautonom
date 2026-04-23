# Extended Builder Codes

Reference: https://docs.extended.exchange/extended-resources/builder-codes

## What it does

Every order placed on Extended via `ExtendedClient` carries two extra fields
(`builder_id`, `builder_fee`). The builder fee is routed to the configured
builder account; Extended pays out once per day at 00:00 UTC.

If a user also qualifies for a trading rebate, the rebate is unaffected.

## Configuration

Non-secret, lives in `app/config.py` (pydantic-settings). All three fields
are overridable via env vars.

| Setting | Default | Env var | Purpose |
|---|---|---|---|
| `extended_builder_enabled` | `True` | `EXTENDED_BUILDER_ENABLED` | Kill-switch |
| `extended_builder_id` | `177174` | `EXTENDED_BUILDER_ID` | Builder account clientId |
| `extended_builder_fee` | `0.00007` | `EXTENDED_BUILDER_FEE` | Fee fraction (0.00007 = 0.007%) |

Builder fields are **not** part of the vault (`_MANAGED_KEYS`) and are not
injected via `/internal/apply-keys`. They come from the container's env /
`app/config.py` defaults at startup.

## Code wiring

`ExtendedClient.__init__` accepts `builder_enabled`, `builder_id`, `builder_fee`
kwargs. A helper `_builder_kwargs()` returns the two kwargs when all three
conditions are true (enabled, id != 0, fee > 0) and an empty dict otherwise.

Call sites that splat `**self._builder_kwargs()` into the x10 SDK:

| Method | SDK call |
|---|---|
| `async_create_post_only_order` | `trading_client.place_order(...)` |
| `async_create_ioc_order` | `trading_client.place_order(...)` |
| `create_aggressive_limit_order` | `trading_client.place_order(...)` |
| `create_limit_order` | `trading_client.place_order(...)` |
| `_build_market_order` | `create_order_object(...)` |

When the kill-switch is off (or id/fee unset), `_builder_kwargs()` returns
`{}` and order placement reverts to stock Extended behaviour — no builder
fee is sent, no side effects.

The x10 SDK (installed version) already supports `builder_id` + `builder_fee`
on both `PerpetualTradingClient.place_order(...)` and
`create_order_object(...)`. No SDK patching required.

## Server wiring

Both `ExtendedClient(...)` construction sites in `app/server.py`
(`_init_exchange_clients`, `_reinit_exchange_clients`) forward the three
settings:

```python
ExtendedClient(
    ...
    builder_enabled=_settings.extended_builder_enabled,
    builder_id=_settings.extended_builder_id,
    builder_fee=_settings.extended_builder_fee,
)
```

## Init log

On successful trading client init, the log line is:

```
ExtendedClient initialised WITH trading (base=..., builder_id=177174, builder_fee=0.00700%)
```

If the kill-switch is off or id/fee are empty:

```
ExtendedClient initialised WITH trading (base=..., builder_code=disabled)
```

## Disabling at runtime

Set in container env and restart (or hot-reload, which triggers a
re-construction via `_init_exchange_clients`):

```bash
EXTENDED_BUILDER_ENABLED=false
```

No code change needed.

## Fee cap caveat

Extended enforces a per-market max builder fee. The SDK exposes
`account.get_fees(market_names=[...], builder_id=...)` which returns
`builder_fee_rate` (the cap). We use a fixed config value (`0.00007`) rather
than fetching the cap per order — this is intentional for simplicity. If the
cap drops below the configured value for some market, orders will be
rejected with an explicit fee-too-high error in the logs. Lower
`EXTENDED_BUILDER_FEE` or disable via the kill-switch.

The default `0.00007` (0.007%) is well below typical caps; the official SDK
example uses `0.0005` (0.05%).

## Verifying on a live order

Either:

- Tail container logs after a trade — no dedicated builder log line, but the
  trading client passes the kwargs through; the response from Extended's
  REST API includes `builderFee` and `builderId` in the placed-order model.
- Query `/user/orders/{id}` via REST: the response contains the builder
  fields when a builder code was attached.

## Files touched

- `app/config.py` — added 3 settings
- `app/extended_client.py` — added constructor kwargs, `_builder_kwargs()`
  helper, and splats into 5 call sites
- `app/server.py` — forwarded settings through both `ExtendedClient(...)`
  construction sites
- `AGENTS.md` §10 — added a one-line note for agents
