# Webhooks

swsdb can tell other systems when a document becomes searchable, fails, or is deleted, so they don't need to poll.

```bash
swsdb webhooks create https://app.example.com/swsdb-events
```
```text
webhook wh_3f9a1c0b2e41 -> https://app.example.com/swsdb-events for document.deleted, document.failed, document.ready
signing secret (shown once): 1n4Q...
```

```python
w = db.create_webhook("https://app.example.com/swsdb-events", events=["document.ready"], collections=["handbook"])
```

## What arrives

```http
POST /swsdb-events
content-type: application/json
swsdb-event: document.ready
swsdb-delivery: 1842
swsdb-signature: sha256=5d1c...

{"at":"2026-10-31T09:12:44Z","chunks":4,"collection":"handbook","document":"refunds","entities":2,
 "event":"document.ready","tenant":"acme","title":"Refund policy","version":2}
```

| Event | When | Extra fields |
|---|---|---|
| `document.ready` | a version became the one queries return | `version`, `title`, `chunks`, `entities` |
| `document.failed` | a version couldn't be indexed (for example, an unreadable file) | `version`, `error` |
| `document.deleted` | a deleted document was removed from every store | `version` |
| `ping` | `swsdb webhooks ping <id>` | `webhook` |

## Guarantees

- **Never lost, never invented.** An event is written in the same Postgres transaction as the change it reports.
- **At least once.** A delivery that doesn't get a 2xx answer within 10 seconds is retried with exponential backoff,
  for about two hours (12 attempts). Retries keep the same `swsdb-delivery` id; use it to ignore duplicates.
- **Signed.** `swsdb-signature` is the HMAC-SHA256 of the raw body with the webhook's secret:

```python
import hashlib, hmac

def valid(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

- **Per tenant.** A webhook belongs to the tenant whose admin key created it and only sees that tenant's documents
  (optionally only some collections).
- **Not into the server.** Webhook URLs may not point at loopback or link-local addresses: the engines inside the
  container, or cloud metadata endpoints. Private networks are fine. The check runs again before every delivery.

## Inspecting deliveries

```bash
swsdb webhooks list                 # with delivered, pending and failed counts
swsdb webhooks deliveries wh_3f9a1c0b2e41
swsdb webhooks delete wh_3f9a1c0b2e41
```

Delivered and failed events are kept for a week.
