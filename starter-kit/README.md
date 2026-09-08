# Stage 2 Exercise Scaffold

This repository contains the environment for the Cloud Interactive Media Group
engineering exercise. There are two systems you do not control: the **USSD
gateway** in front of your service, and the **telecom operator** behind it.

**Do not modify anything inside `mock-operator/` or `ussd-gateway/`.**

## Running it

```bash
docker compose up --build
```

- Operator API: `http://localhost:9000`
- PostgreSQL: `localhost:5432` (user `vend`, password `vend`, database `vend`)
- API key: `sk_test_operator_7f3a9c1e` (send as `X-Api-Key` header)

Add your own service to `docker-compose.yml`. The whole system must come up with
a single `docker compose up`.

Drive your service from the gateway side with:

```bash
python ussd-gateway/driver.py --base-url http://localhost:8000 --scenario all
```

---

## USSD gateway contract

`ussd-gateway/driver.py` is the gateway. It is not a service; it POSTs session
callbacks at **your** service and prints what came back, exactly as an operator
gateway or aggregator would.

### It POSTs to `{your service}/ussd`, form-encoded

| Field | Meaning |
|---|---|
| `sessionId` | Opaque, stable for the life of one session |
| `serviceCode` | The short code dialled, e.g. `*384*7000#` |
| `phoneNumber` | MSISDN in E.164, e.g. `+2348031234567` |
| `text` | Accumulated user input, `*`-delimited. First callback sends `text=''`, then `1`, then `1*500`, then `1*500*1` |

### Your service replies in plain text

```
CON <message>    keep the session open and show <message>
END <message>    terminate the session and show <message>
```

Anything else, a non-200, or a response slower than **8 seconds** is treated by
the gateway as a failed session, and the subscriber sees a network error. The
gateway does not care why.

### Scenarios the driver will run against you

| Scenario | What it does |
|---|---|
| `happy` | A clean walk through the menu to a completed vend |
| `gateway-retry` | The gateway re-sends the same confirm callback three times |
| `double-tap` | Two identical confirm callbacks arrive concurrently on one session |
| `abandoned` | Subscriber walks away mid-flow and no further callbacks arrive |
| `resume` | Session dropped, subscriber redials with a new `sessionId` |
| `out-of-order` | A confirm callback arrives for a session that never chose an amount |
| `bad-input` | Invalid menu options, non-numeric, negative and absurd amounts |
| `slow` | Times your confirm response against the 8-second gateway window |

The gateway retries and the double-tap are not edge cases. They happen every day
on live short codes, and each one is a chance to debit a subscriber twice.

---

## Operator API contract

This is the integration document the partner gave you. It is accurate but
incomplete, which is normal.

### `POST /v1/vend`

```json
{
  "client_ref": "CIP-20260811-000001",
  "msisdn": "08031234567",
  "network": "MTN",
  "amount_minor": 50000
}
```

`amount_minor` is in kobo. `network` is one of `MTN`, `AIRTEL`, `GLO`, `9MOBILE`.

Responses:

| Code | Body `status` | Meaning |
|------|---------------|---------|
| 200 | `SUCCESSFUL` | Vend completed. `operator_ref` returned. |
| 200 | `FAILED` | Vend did **not** happen. See `reason_code`. |
| 400 | `REJECTED` | Malformed request. Do not retry unchanged. |
| 429 | `REJECTED` | Rate limited. `Retry-After` header supplied. |
| 500 | `UNKNOWN` | Operator-side error. Nothing was committed. |
| 504 | `UNKNOWN` | Gateway timeout. **The vend may or may not have committed.** |

**The operator does not support idempotency keys.** Sending the same
`client_ref` twice creates two separate vends and debits the customer twice.

### `GET /v1/status?client_ref=...`

Look up what the operator thinks happened. Rate limited to **10 requests per
minute**, and returns `status: "UNKNOWN"` for a proportion of lookups even when
a vend exists. Do not treat `UNKNOWN` as `NOT_FOUND`.

### `GET /v1/settlement/{YYYY-MM-DD}`

Returns `text/csv`. This is the operator's book of record and takes precedence
over anything the API told you in real time.

```
operator_ref,client_ref,msisdn,network,amount_minor,status,completed_at
```

The settlement file does not always agree with the API responses you received.
Working out how, and what to do about it, is part of the exercise.

### `POST /v1/_admin/reset`

Clears operator state. Useful between test runs. Not available in production.

---

## Determinism

Operator behaviour is derived from a hash of your `client_ref`, so the same
`client_ref` always produces the same outcome. Your tests are reproducible, and
so is our review. Vary your `client_ref` values when testing so that you
exercise the full range of failure modes.
