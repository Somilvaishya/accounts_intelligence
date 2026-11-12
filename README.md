# Accounts Intelligence

Group-level accounting intelligence for a group of companies that runs **one ERPNext site per company**.

Each company site (a **client**) sends its accounting facts to one group site (the **server**). The server matches them across companies and shows the finance head where the books disagree. For example, company A booked a sale to company B, but B never booked the matching purchase.

Works on **Frappe / ERPNext v15 and v16**, and runs on Frappe Cloud with no infrastructure changes.

## Status

| Part | Status |
|---|---|
| Site-to-site authentication and settings | **Built and tested** |
| Sending accounting data from clients to the server | **Built and tested** |
| Inter-company reconciliation: invoices, payments and balances | **Built and tested** |
| Reconciliation frontend at `/intelligence` on the server | **Built** |
| Related-party transactions (RPT) report and register | **Built and tested** |
| Open items work queue: assign, explain, ask AI | **Built and tested** |
| Sync health: site states, warnings and activity | **Built and tested** |
| Assistant: questions about the reconciliation, using the AI Interface app | **Built and tested** |

The rest of this README describes what is built today: the trust layer every later part relies on, the data sync that runs over it, and the reconciliation that reads the synced data.

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
- **Every rejection looks the same.** The caller always gets HTTP 401 `not_authorised`. The real reason is written only to the **Accounts Intelligence Auth Log** on the server, which keeps refusals and other events for 180 days. Routine successes are kept short: an accepted ping is not logged (the site row's Last Seen records it), and accepted sync calls are cleared after 7 days by a daily job.
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
| Journal Entry | one row per entry and watched party, with the net amount in company currency |
| Balance with each group company and related party | sent only on the days it changes |

**How group companies are found.** Each company is identified by its PAN, which every one of its GSTINs contains. A site represents one company: the **Default Company** in Global Defaults, or the only company on the site. On every run the client registers that company with the server, and the server answers with the **watch list**: the PAN of every group company, and of every party in the related-party register that is related to this company. The client then sends only documents whose customer or supplier has one of those PANs, read from the party's `pan`, `gstin` (India Compliance) or `tax_id` field, or from the GSTIN on one of its addresses.
- A party with the site's own PAN is the company itself under another GSTIN, so its documents are not sent.
- A party is never matched by name. One named like a group company but without its PAN or GSTIN is listed in **Last Sync Result**, so it can be fixed.

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

## How reconciliation works

For each pair of group companies, the server compares what each one recorded about the other. It is worked out on request from the synced rows, so nothing extra is stored and nothing goes stale. Cancelled documents are left out.

| Check | How lines are paired |
|---|---|
| Invoices | The seller's sales invoice with the buyer's purchase invoice: first by the buyer's **bill number**, which names the seller's invoice in full (`SI090126-0317`) or by its trailing number (`0262`); then, for a purchase invoice without a usable bill number, the one sales invoice of the same date and amount. When more than one fits, nothing is guessed. |
| Payments and journals | Each document counts as the amount it debits the other company's account. A payment one company makes is a receipt for the other, so a matching pair adds up to zero, within 3 days of each other. A payment can match a journal entry on the other side. A journal entry that nets to zero only moves an amount between the company's own customer and supplier accounts, so it is left out. |
| Balances | Each company's latest balance with the other. They should add up to zero. |

Each line is **Matched**, **Amount differs**, **Date differs**, or **Only at** one of the two companies. Differences under ₹1 count as matched, because the two books round taxes differently.

Over the whole history, the open lines should explain the balance difference. What they do not explain is shown as **Unexplained**: documents older than the sync start date, or entries booked against a party that has no PAN.

**The frontend.** On the server, open `/intelligence` (System Manager only).
- **Dashboard:** the total balance gap, open items, match rate and unexplained amount; charts of the gap per company pair, lines per month, open items by age and the group as a network; then every pair with its matched and open counts. The sidebar lists the pairs and shows how the company sites are syncing.
- **Pair page:** both companies' balances, what makes up the difference, open items by age, then every line side by side, filtered by status, type, period or text, with a CSV download. Each document links to the company site that holds it.
- Light and dark themes, and the assistant in a side panel.

It reads two endpoints, `accounts_intelligence.reconcile.dashboard` and `accounts_intelligence.reconcile.pair`, both GET and System Manager only.

### Sync health

**Sync health** in the frontend shows every company site on the server's list, on one page.

**Each site's state:**
- **Healthy**
- **Late:** no sync for twice the interval
- **Never synced**
- **Pending**
- **Disabled**
- **Revoked**

**For each site:**
- its companies and site address
- the last sync and the size of its last batch
- the daily check, with an amber flag when months differ
- a bar per hour for the last 24 hours of accepted sync calls, from the Auth Log
- refused calls
- how many of its documents are stored here

**Warnings** come from each site's own **Last Sync Result**. A client sends it with its next config call, and the server keeps it on the site's row. They include:
- a run that failed;
- a party named like a group company or related party but without a matching PAN;
- a daily check that differs.

Each warning clears on the site's next run once it is fixed there.

Endpoint: `accounts_intelligence.health.sites` (GET, System Manager only).

### Open items

**Open items** in the frontend is the work queue for the lines that do not match, across every company pair.

**Filters:** status, company pair, type, age, who it is assigned to, and text.

**Each line shows:**
- its age in days;
- both companies' documents, or which side has not recorded it;
- the amount at stake.

**Actions on a line:**
- **Assign** it to a System Manager.
- Mark it **In progress**.
- **Explain** it with a reason (timing difference, booked under another party, will be booked, disputed, not an inter-company transaction, other) and a note.
- **Ask AI** about it.

**How reviews are stored.** The lines are still worked out by the reconciliation. Only a review someone saves is stored, in **Accounts Intelligence Open Item**, keyed by the line's pair and documents, with its change history. An explained line still counts as open, because the two books still differ. The pair page shows its review, and the dashboard counts it as explained.

Endpoints: `accounts_intelligence.open_items.items` (GET) and `accounts_intelligence.open_items.review` (POST), System Manager only.

## Related-party transactions

The report for Ind AS 24 and section 188 of the Companies Act: for each group company, what it did with each of its related parties in a period. Open **Related parties** in the frontend, choose the company and period (the financial year to date by default), and download it as CSV.

**Who counts as a related party.**
- **Other group companies:** always, with no setup.
- **Everyone else:** an entry in **Accounts Intelligence Related Party** on the server, by PAN (or a GSTIN, from which the PAN is taken). This covers key managerial personnel, their relatives, the firms and companies they control, a holding company and so on.
  - **Related To:** empty when the party is related to every group company, or one company otherwise.
  - **Related From / Related Until:** limit the relationship to dates, such as a director's tenure.

The register drives the sync. Each client watches the PANs of its own related parties too, so its documents with a director's firm reach the server like those with a group company. Adding a party makes the clients send its older documents on their next run. A related party has no books here, so it is never part of the reconciliation, which stays between group companies.

**What the report shows** per company and party, in company currency:
- sales and purchases without tax and net of returns;
- money received and paid;
- the net of journal entries;
- the balance on the period's last day.

Cancelled documents are left out. Transactions outside a party's Related From / Until dates are left out. Remuneration paid through payroll is not synced, so it is not included.

Endpoint: `accounts_intelligence.rpt.report` (GET, System Manager only).

## The assistant

Questions in plain language about the reconciliation, such as "which payments are recorded on one side only?" or "why do GGIL and GGSPL differ?", answered with the lines behind the answer.

**The AI model comes from the [AI Interface](https://github.com/sagarrgarg/ai-interface) app.** Its provider, model, budget, rate limit and call log apply, and its AI Call Log shows every call under the app `accounts_intelligence`. AI Interface is needed for the assistant only: without it, or without a default provider in **AI Settings**, the panel says what is missing and the rest of the app works as before.

A question costs at most two calls:
1. **Plan.** The model picks one tool and its arguments:
   - a group summary, or one pair's summary;
   - open items, by company, type, status, recorded by, period, age or amount;
   - a document, by its number, bill or bank reference;
   - an email draft from one company's accounts team to another's;
   - related-party transactions for a company and period.
2. **Answer.** `assistant.py` runs that tool on the reconciled data, then the model words the answer from the result only. Lines carry their review, so an answer can say that a line is already explained.

**Email drafts.** "Draft an email to GGSPL" lists three things:
- the documents only in our books, which they have to book;
- the documents whose amount or date differs;
- the documents only in their books, which we will book.

Each list item gives the document number, date and amount, with both balances above the lists. Lines already explained are left out. The draft is shown with **Copy** and **Open in email**, and is never sent from the app: the person chooses the recipients and can change the text in their own email. Under any answer about one pair, **Draft email to …** asks for one in a click.

**What keeps it safe.**
- The model never writes a query, and sees nothing but the tool's result.
- Amounts and who owes whom reach the model already worked out, so it does no arithmetic.
- An unknown tool, company or argument is refused rather than guessed.
- Only a System Manager can ask, and nothing is written.
- The conversation is kept in the browser, and the last few turns go back with each question for follow-ups.

Endpoints: `accounts_intelligence.assistant.status` (GET) and `accounts_intelligence.assistant.ask` (POST).

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
  reconcile.py            invoice, money and balance matching; the frontend's two endpoints
  assistant.py            the assistant: plan, run one tool, answer (model via AI Interface)
  rpt.py                  related-party transactions report
  open_items.py           the open items work queue and its reviews
  health.py               sync health: each site's state, warnings and activity
  overrides.py            blocks reading the private keys through frappe.client.get_password
  www/intelligence.py     serves the frontend page (login, System Manager, server mode)
  public/frontend/        the built frontend, committed so no site needs Node to install the app
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
    accounts_intelligence_related_party/  server: the related-party register
    accounts_intelligence_open_item/      server: reviews of open reconciliation lines
  tests/test_auth.py          authentication
  tests/test_key_rotation.py  key rotation and regeneration
  tests/test_hardening.py     security audit fixes
  tests/test_sync.py          data sync
  tests/test_reconcile.py     reconciliation
  tests/test_assistant.py     the assistant, with the AI calls replaced
  tests/test_rpt.py           related-party register, sync and report
  tests/test_open_items.py    the open items queue and reviews
  tests/test_health.py        sync health
frontend/                 React + Vite + Tailwind source of the /intelligence page
```

### Changing the frontend

```bash
cd apps/accounts_intelligence/frontend
yarn install
yarn dev     # http://localhost:8081/intelligence, API calls go to the bench on port 8000
yarn build   # type-checks, writes accounts_intelligence/public/frontend and www/intelligence.html
```

For `yarn dev`, log in on the bench's own address first. Set `BENCH_URL` if the bench is not on `http://127.0.0.1:8000`. Commit the build output together with the source change.

## Tests

```bash
bench --site your-test-site run-tests --app accounts_intelligence
```

The 126 tests build real HTTP requests and run them through the verification code, and run full sync cycles against the server handlers. They cover:
- URL rules;
- tampered bodies and paths, and requests aimed at a different server;
- replayed nonces, stale timestamps and the wrong key;
- unknown, disabled, Pending and Revoked sites;
- the connect ownership check;
- the uniform 401 and its log entry;
- the settings guards;
- key rotation: a successful switch, the old key alone being refused without the site URL, the proof bound to the request, and resuming an interrupted rotation without losing the new key;
- Regenerate Key on a client and on the server;
- data sync: only group-company documents are sent, resent batches are stored once, older copies never overwrite newer ones, a failed send keeps the watermark, the start date and interval, the daily check and resync;
- reconciliation: invoices paired by full or trailing bill number and by date and amount, no guess when several fit, payments paired with payments or journals within 3 days, set-off journals left out, latest balances and the unexplained amount, System Manager only.

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
