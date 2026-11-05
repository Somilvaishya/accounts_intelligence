# Accounts Intelligence

Group-level accounting intelligence for a group of companies that runs **one ERPNext site per company**.

Each company site (a **client**) sends its accounting facts to one group site (the **server**). The server matches them across companies and shows the finance head where the books disagree. For example, company A booked a sale to company B, but B never booked the matching purchase.

Works on **Frappe / ERPNext v15 and v16**, and runs on Frappe Cloud with no infrastructure changes.

## Status

| Part | Status |
|---|---|
| Site-to-site authentication and settings | **Built and tested** |
| Sending accounting data from clients to the server | **Built and tested** |
| Related party and inter-company matching | Planned |
| Findings dashboard for the finance head | Planned |
| Chat over the group's data, limited to each user's permissions | Planned |

The rest of this README describes what is built today: the trust layer every later part relies on, and the data sync that runs over it.

## How authentication works

There are **no integration users, no API keys and no shared secrets**. The only thing an administrator configures on the server is a whitelist of client site URLs.

- **Every site has its own Ed25519 key pair.** It is created on the site's first save of the settings. The private key is stored encrypted in a Password field and never leaves the site. Only the public key and its fingerprint are shared.
- **The server stores only public keys.** Even a full copy of the server's database cannot be used to send data as a client.
- **Connecting a client proves who owns the URL.**
  1. The client reads the server's public key and pins it. If the key ever changes, the client stops.
  2. The client sends a connect request signed with its own key.
  3. The server checks the URL is on its whitelist, then fetches the key published at that URL itself and requires an exact match.
  4. An administrator compares the fingerprints shown on both sites and approves the site.
- **Every request after that is signed.** The signature covers the method, path, target server, sending site, a timestamp, a single-use nonce and the SHA-256 of the body. The server refuses a request if any of these is wrong:
  - the site isn't whitelisted, enabled and Active;
  - the signature doesn't match the site's stored key;
  - the timestamp is more than 5 minutes off;
  - the nonce has been used before (checked atomically in Redis).
- **Replies are signed too.** Each reply is bound to the request's nonce, so the client can verify that it came from the pinned server key and belongs to that request.
- **Every rejection looks the same.** The caller always gets HTTP 401 `not_authorised`. The real reason is written only to the **Accounts Intelligence Auth Log** on the server, which is kept for 180 days.
- **HTTPS only in production.** Plain `http://` is accepted only when the "Allow Plain HTTP (Testing Only)" switch is on **and** the bench is in developer mode.

A captured request cannot be altered, replayed, or sent to another server or endpoint.

### Replacing keys

- **Rotate Key (client, while connected).** The client creates a new key pair and sends `rotate_key`, signed with its current key. The request carries the new public key and a statement signed by the new key. That statement binds the new key to the old key, the site and this request's nonce. The server switches to the new key only when all three hold:
  - the request is signed with the current key;
  - the new key signed the statement;
  - the client's own URL publishes the new key as its `next_public_key`.

  Someone who stole only the old private key cannot pass the last check. The site stays approved. If the reply is lost, clicking Rotate Key again checks whether the server already switched, finishes the rotation if it did, and otherwise starts a new one. It never discards a new key the server may already hold.
- **Regenerate Key (either side).** Replaces the key pair outright, for when the private key may be known to someone else.
  - On a client, the server must reset that site's row and approve it again.
  - On the server, each client resets its connection and connects again after checking the new fingerprint. Client approvals stay valid.

### Protocol AI1: request headers

| Header | Content |
|---|---|
| `X-AI-Site` | Sender's canonical URL; must match a whitelisted row exactly |
| `X-AI-Key` | SHA-256 of the sender's public key |
| `X-AI-Timestamp` | Unix seconds, accepted within ±300 s |
| `X-AI-Nonce` | 32 hex characters, single use |
| `X-AI-Signature` | Base64 Ed25519 signature over the canonical string |

The signed canonical string:

```
AI1
POST
/api/method/accounts_intelligence.api.<method>
<server URL>
<sending site URL>
<timestamp>
<nonce>
<sha256 of the raw body>
```

### Endpoints

| Endpoint | Method | Authentication | Rate limit per IP |
|---|---|---|---|
| `accounts_intelligence.api.identity` | GET | None (public data only) | 30 / min |
| `accounts_intelligence.api.connect` | POST | Signature and URL ownership check | 10 / min |
| `accounts_intelligence.api.ping` | POST | Signature from an Active site | 60 / min |
| `accounts_intelligence.api.rotate_key` | POST | Signature with the current key, proof from the new key, and the new key published at the site URL | 10 / min |
| `accounts_intelligence.api.sync_config` | POST | Signature from an Active site | 30 / min |
| `accounts_intelligence.api.sync` | POST | Signature from an Active site | 300 / min |

The buttons on the settings form call `accounts_intelligence.auth.admin.*` and need the System Manager role.

## How data sync works

Each client sends the **headers** of its documents with **other group companies** to the server. Nothing else leaves the site: no line items, no attachments, no documents with outside customers or suppliers, and nothing is written to file storage on either side.

**What is sent**

| Data | Fields |
|---|---|
| Sales Invoice, Purchase Invoice, Payment Entry (submitted or cancelled) | company, document, posting date, party, PAN of the counterparty, grand total, net total, tax, currency, total in company currency, return and cancelled flags, supplier invoice no, payment type, reference no |
| Journal Entry | one row per entry and group-company party, with the net amount in company currency |
| Balance with each group company | sent only on the days it changes |

**How group companies are found.** Each company is identified by its PAN, which every one of its GSTINs contains. On its first sync a client registers its companies with the server. The server answers with the **watch list**: the PAN of every group company. The client then sends only documents whose customer or supplier has one of those PANs, read from the party's `pan`, `gstin` (India Compliance) or `tax_id` field.

**How it stays correct**
- Each document type has a watermark (last `modified` and name). It moves only after the server's signed reply accepts the batch. A failed run sends the same rows again, and the server's upsert makes that harmless.
- Rows are keyed on company, document type, document and party, so a resent row lands on the same record. An older copy of a document never overwrites a newer one.
- A client can only write rows for the companies registered to its own site. The site comes from the request signature, not from the body.
- **Daily check:** once a day the client sends its count and total per document type and month. The server compares them with what it stored and records the result in the site row's **Last Daily Check**. If the server is missing documents, the client sends that month again. If the server holds more (documents deleted on the client), the difference is only reported.

**Rules set on the server** (Settings → Data Sync Rules):

| Setting | Effect |
|---|---|
| Sync Documents From | Documents posted earlier are not sent. Empty means full history. Moving it earlier makes clients send the older documents; moving it later keeps what is stored. Balances are always full balances. |
| Sync Every | 5 minutes to 1 day. Clients check every 5 minutes and send only when this interval has passed. |
| Sync Documents From on a site row | Overrides the group date for that site, for example a company that joined the group later. |

**Where it is stored on the server:** **Accounts Intelligence Entity** (one row per group company, named by PAN), **Accounts Intelligence Transaction** (one row per document, or per party for a journal entry) and **Accounts Intelligence Balance**. On the client, only the Data Sync section of the settings changes: Last Sync On, Last Sync Result and a hidden state field.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/Somilvaishya/accounts_intelligence.git --branch development
bench --site your-site install-app accounts_intelligence
```

It needs `frappe` and `erpnext`. No extra Python or system packages are required: Ed25519 comes from the `cryptography` library that Frappe already ships with.

On **Frappe Cloud**, add the app from GitHub to each bench group, v15 or v16, using the same branch. Supported versions are declared in `pyproject.toml` under `[tool.bench.frappe-dependencies]`.

## Setup

Open **Accounts Intelligence Settings** on each site.

### On the group site (server)

1. Set **Mode** to **Server** and **This Site's URL** to its real address, for example `https://group.example.com`, then save. This creates the site's key pair and shows its fingerprint.
2. In **Allowed Sites**, add each company site's exact URL, then save. Each row starts as **Pending**.

### On each company site (client)

1. Set **Mode** to **Client**, **This Site's URL** to its own address, and **Server URL** to the group site's address, then save.
2. Click **Connect to Server**. The status becomes **Waiting for approval**.

### Approve

1. On the server, reload the settings. The company's row now shows a key fingerprint.
2. Check it matches **This Site's Key Fingerprint** on the company site.
3. Select the row and click **Approve Selected**.
4. On the company site, click **Test Connection**. The status should read **Connected**.

### Start data sync

1. On the server, set **Sync Documents From** and **Sync Every** under **Data Sync Rules**, then save.
2. On each company site, make sure every company has a PAN or GSTIN, and that customers and suppliers which are group companies have one too.
3. Tick **Send Data to Server** and save. The scheduler sends the first batch within 5 minutes, or click **Sync Now**. **Last Sync Result** shows what was sent.

### Day-to-day

| Situation | What to do |
|---|---|
| Remove a company | Server: **Revoke Selected**. It takes effect immediately. |
| Replace a company's key (routine) | Client: **Key → Rotate Key**. No new approval is needed. If it is interrupted, click it again. |
| A company's key may be known to someone else | Client: **Key → Regenerate Key**. Server: **Reset Selected** on its row. Client: **Connect**. Server: compare fingerprints and approve. |
| The server's key may be known to someone else | Server: **Key → Regenerate Key**. Each client: **Reset Connection**, check the new server fingerprint, then **Connect**. Approvals stay valid. |
| Company site changes URL | Edit the row's URL (it resets to Pending), update the client's own URL, then connect and approve. |
| Client says "the server's key has changed" | Treat it as a security event. Reset only after the server's admin confirms they replaced the key on purpose. |
| Monitoring | Review **Rejected** entries in **Accounts Intelligence Auth Log** on the server. The server's settings page lists sites that have not synced for more than twice the interval. |
| Server data looks incomplete for a company | Check the row's **Last Daily Check**. To send everything again, select the row and click **Resync Selected**; nothing stored is deleted. |
| Backups | Keep a copy of each site's `encryption_key`. The private key cannot be decrypted after a restore without it. |

## Testing two sites on one local bench

`bench start` serves **only the default site**: with no `--site`, `bench serve` takes `default_site` and answers every host as that site. To run a server and a client side by side, start a second web server pinned to the other site:

```bash
# terminal 1: bench start serves the default site (for example the server) on port 8000
bench start

# terminal 2: the client site on its own port
bench --site client-site.local serve --port 8001
```

Then set up the two sites with these addresses:
- server: `http://127.0.0.1:8000`
- client: `http://localhost:8001`

Use two different host names so the browser keeps the two logins apart, and turn on **Allow Plain HTTP (Testing Only)** on both. Do not run `bench use` while testing, because it changes which site port 8000 serves.

## Project structure

```
accounts_intelligence/
  api.py                  public endpoints: identity, connect, ping, rotate_key, sync_config, sync
  overrides.py            blocks reading the private keys through frappe.client.get_password
  sync/
    client.py             collector, watermarks, daily check, scheduled job
    server.py             watch list, validation, bulk upsert, daily comparison
  auth/
    urls.py               canonical site URLs and the HTTPS rule
    keys.py               Ed25519 keys, signing, key id, fingerprint
    signing.py            canonical request string and signed replies
    server.py             request verification, nonce claim, handlers
    transport.py          outbound HTTPS calls with reply verification
    admin.py              settings form actions (System Manager only)
    audit.py              Auth Log writer
  accounts_intelligence/doctype/
    accounts_intelligence_settings/       Single: mode, keys, connection
    accounts_intelligence_allowed_site/   child table: the URL whitelist
    accounts_intelligence_auth_log/       security event log
    accounts_intelligence_entity/         server: one row per group company (PAN)
    accounts_intelligence_transaction/    server: synced document headers
    accounts_intelligence_balance/        server: balances with group companies
  tests/test_auth.py          authentication
  tests/test_key_rotation.py  key rotation and regeneration
  tests/test_hardening.py     security audit fixes
  tests/test_sync.py          data sync
```

## Tests

```bash
bench --site your-test-site run-tests --app accounts_intelligence
```

The 85 tests build real HTTP requests and run them through the verification code, and run full sync cycles against the server handlers. They cover:
- URL rules;
- tampered bodies and paths, and requests aimed at a different server;
- replayed nonces, stale timestamps and the wrong key;
- unknown, disabled, Pending and Revoked sites;
- the connect ownership check;
- the uniform 401 and its log entry;
- the settings guards;
- key rotation: a successful switch, the old key alone being refused without the site URL, the proof bound to the request, and resuming an interrupted rotation without losing the new key;
- Regenerate Key on a client and on the server;
- data sync: only group-company documents are sent, resent batches are stored once, older copies never overwrite newer ones, a failed send keeps the watermark, the start date and interval, the daily check and resync.

They pass on v15 and v16.

## Known limitations

- The server's key cannot be rotated in place. Replacing it means Regenerate Key on the server and a reconnect from each client.
- The client trusts the server key it sees first. The fingerprint comparison at approval is the check for that.
- Redis must be available. If it is down, requests are refused rather than allowed.
- Site clocks must agree within 5 minutes.
- A party without a PAN or GSTIN is never recognised as a group company, so its documents are not sent.
- A document cancelled and deleted between two syncs is not removed on the server; the daily check reports it.

## Contributing

This app uses `pre-commit` for formatting and linting:

```bash
cd apps/accounts_intelligence
pre-commit install
```

It runs ruff, eslint, prettier and pyupgrade.

## License

MIT
