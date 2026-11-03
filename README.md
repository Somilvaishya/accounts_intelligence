# Accounts Intelligence

Group-level accounting intelligence for a group of companies that runs **one ERPNext site per company**.

Each company site (a **client**) sends its accounting facts to one group site (the **server**). The server matches them across companies and shows the finance head where the books disagree. For example, company A booked a sale to company B, but B never booked the matching purchase.

Works on **Frappe / ERPNext v15 and v16**, and runs on Frappe Cloud with no infrastructure changes.

## Status

| Part | Status |
|---|---|
| Site-to-site authentication and settings | **Built and tested** |
| Sending accounting data from clients to the server | Planned |
| Related party and inter-company matching | Planned |
| Findings dashboard for the finance head | Planned |
| Chat over the group's data, limited to each user's permissions | Planned |

The rest of this README describes what is built today: the trust layer every later part relies on.

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

The buttons on the settings form call `accounts_intelligence.auth.admin.*` and need the System Manager role.

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

### Day-to-day

| Situation | What to do |
|---|---|
| Remove a company | Server: **Revoke Selected**. It takes effect immediately. |
| Replace a company's key | Server: **Reset Selected**. Client: **Reset Connection**, then **Connect**. Server: compare fingerprints and approve. |
| Company site changes URL | Edit the row's URL (it resets to Pending), update the client's own URL, then connect and approve. |
| Client says "the server's key has changed" | Treat it as a security event. Reset only after the server's admin confirms they replaced the key on purpose. |
| Monitoring | Review **Rejected** entries in **Accounts Intelligence Auth Log** on the server. |
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
  api.py                  public endpoints: identity, connect, ping
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
  tests/test_auth.py      27 tests
```

## Tests

```bash
bench --site your-test-site run-tests --app accounts_intelligence
```

The 27 tests build real HTTP requests and run them through the verification code. They cover:
- URL rules;
- tampered bodies and paths, and requests aimed at a different server;
- replayed nonces, stale timestamps and the wrong key;
- unknown, disabled, Pending and Revoked sites;
- the connect ownership check;
- the uniform 401 and its log entry;
- the settings guards.

They pass on v15 and v16.

## Known limitations

- There is no in-place key rotation yet. Replacing a key means a reset on both sides, then a new connect and approval.
- The client trusts the server key it sees first. The fingerprint comparison at approval is the check for that.
- Redis must be available. If it is down, requests are refused rather than allowed.
- Site clocks must agree within 5 minutes.

## Contributing

This app uses `pre-commit` for formatting and linting:

```bash
cd apps/accounts_intelligence
pre-commit install
```

It runs ruff, eslint, prettier and pyupgrade.

## License

MIT
