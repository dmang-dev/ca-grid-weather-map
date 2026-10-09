# pinger/

Cloudflare Worker that keeps the hosted map fresh when GitHub's scheduler
misses a tick.

## Why this exists

`deploy-pages.yml` asks for a rebuild every 2 hours. GitHub's `schedule`
trigger is best-effort and routinely under-delivers — over one 39-hour sample
it produced 13 runs against an expected 20, averaging ~3 h apart, including a
6.25 h gap. Cloudflare's cron triggers are dependable, so this worker fills the
gaps.

It checks the deployed `manifest.json` every 30 minutes and dispatches
`deploy-pages.yml` **only** when the snapshot is older than
`MAX_AGE_MINUTES` (110). The workflow's own cron stays armed as a backstop;
checking first is what stops the two schedulers queueing duplicate deploys.

Every tick also checks that its GitHub token still works and that the
workflow is still enabled, and **throws** when either doesn't, so the
invocation is recorded as an error rather than a quiet success. That is the
lesson of the [September 2026 outage](#what-stopped-it-last-time).

## A note on `npm audit`

`npm audit` reports a few advisories against `undici`, reached via
`wrangler → miniflare → undici`. Two things to know before chasing them:

- **Nothing vulnerable ships.** miniflare is Wrangler's *local* dev sandbox.
  The deployed bundle is ~8 KiB of this worker and contains no `undici` at
  all — verify with `npx wrangler deploy --dry-run --outdir=.wrangler/dryrun`
  and grep the output.
- **npm's suggested fix is behind us.** It proposes wrangler 4.35.0; this
  pins ^4.119.0, which is newer. There is nothing further to upgrade to, so
  `npm audit fix --force` would only downgrade Wrangler.

## Setup

You need a Cloudflare account and a GitHub token. Both stay yours — the token
goes straight into Wrangler's secret store and never appears in this repo.

**1. Create a fine-grained personal access token**

<https://github.com/settings/personal-access-tokens/new>

- Repository access → **Only select repositories** → `ca-grid-weather-map`
- Permissions → Repository permissions → **Actions: Read and write**
  (that is what `workflow_dispatch` requires — nothing else is needed)
- **Pick a long expiry and write the date down.** GitHub's default is 30
  days, and that default is what stopped the pinger for 33 days in
  Sept 2026. The Worker warns in its logs from 7 days out; once the token
  lapses, every tick fails as an exception until you renew it with
  `npx wrangler secret put GITHUB_TOKEN`.

**2. Install and authenticate** (Wrangler 4; needs Node 20+)

```bash
cd pinger
npm install
npx wrangler login
```

**3. Store the token as a secret**

```bash
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret list          # confirm it actually attached
```

Paste the token at the prompt. It is write-only from then on — not readable
from the dashboard, not in `wrangler.toml`, not in git.

**Set the secret *after* the first successful deploy.** Secrets attach to a
deployed Worker, so running this while earlier deploys were still failing
leaves nothing to attach to. That used to be quiet — the cron fired on time and
every invocation returned `Ok`. It now throws on every tick, logging

```json
{"cron":"*/30 * * * *","action":"error","reason":"GITHUB_TOKEN secret is not set","ageMinutes":47,...}
```

but `wrangler secret list` is still the cheap check. A Worker that runs
perfectly while doing nothing is worse than one that crashes.

**4. One-time: make sure the account has a workers.dev subdomain**

Open <https://dash.cloudflare.com/> → **Workers & Pages**. Visiting the landing
page for the first time provisions the account's `*.workers.dev` subdomain.

This is required even though this Worker sets `workers_dev = false` and has no
public URL: Cloudflare's *schedules* API refuses to register cron triggers on
an account without one, failing with

```
You need a workers.dev subdomain in order to proceed. [code: 10063]
```

The two settings are independent — the account has a subdomain, and each
Worker chooses whether to publish to it. `workers_dev = false` means this one
does not, so nothing here becomes publicly reachable.

**5. Deploy**

```bash
npx wrangler deploy
```

A clean deploy ends with the cron schedule listed. If it says
`No targets deployed` **and** reports a trigger error, the subdomain step above
hasn't been done — the code uploads fine but the schedule silently doesn't
register, so the Worker never runs.

## Verifying

```bash
npx wrangler tail          # live logs; one line per cron tick
```

Logs are also persisted to Workers Logs (`[observability]` in
`wrangler.toml`): dashboard → Workers & Pages → `cagrid-deploy-pinger` →
Logs, kept for 3 days on the free plan.

Each tick logs its decision, along with what the token check saw:

```json
{"cron":"*/30 * * * *","action":"skipped","reason":"still fresh","ageMinutes":47,"manifestStatus":200,"maxAge":110,"tokenStatus":200,"tokenValid":true,"tokenExpires":"2027-09-30T00:00:00.000Z","tokenExpiresInDays":355.3,"workflowState":"active"}
{"cron":"*/30 * * * *","action":"dispatched","ageMinutes":118,...,"dispatched":true,"status":204}
```

Those two go to `console.log`. Anything else goes to `console.error` and the
handler throws, so the invocation is recorded as an exception:

- `"action":"error"` — the token was rejected (401: expired or revoked;
  403/404: the workflow is gone, or SSO or an IP allowlist blocks it), the
  workflow isn't `active`, or the secret isn't set. Raised even when the
  snapshot is fresh.
- `"action":"failed"` — the dispatch itself didn't go through; `status` and
  `error` carry GitHub's response. A 403 here, with the token check passing,
  means the token lacks **Actions: Read and write**: the check is a read, and
  on a public repo any token can read.
- `"action":"exception"` — something `check()` didn't expect rejected, such
  as a dispatch request that got no answer; `error` says what.

Not every failed token check is the token's fault. A 5xx, a 429, a 403 with
`x-ratelimit-remaining: 0`, a 200 whose body can't be read, or no answer at all
says GitHub (or the path to it) is unwell, not that the token is dead, so it is
not an `error`: the tick logs a `console.warn` ("token check inconclusive"),
reports `"tokenValid":null`, and decides on freshness alone. A stale snapshot
still gets its dispatch attempt, and if that fails too the tick throws as
`failed` — don't rotate the token on the strength of a `tokenStatus` in the
500s.

Within 7 days of the token's expiry each tick also logs a `console.warn`
naming the date.

This is a **cron-only** Worker: `workers_dev = false` and no routes, so it has
no public URL at all. Cloudflare invokes the schedule directly. (Wrangler 4
refuses to deploy a route-less Worker unless that flag says so explicitly —
answering "no" to its workers.dev prompt without it just errors out.)

The `fetch` handler is still there as a read-only status view, reachable
locally via `npx wrangler dev`:

```json
{"nextTick":"skipped","reason":"still fresh","ageMinutes":47,"manifestStatus":200,"maxAgeMinutes":110,"wouldDispatch":false,"tokenConfigured":true,"tokenValid":true,"tokenStatus":200,"tokenExpires":"2027-09-30T00:00:00.000Z","tokenExpiresInDays":355.3,"workflowState":"active",...}
```

It runs the same token check as the cron path, so `tokenValid` means the token
authenticates and can see the workflow, not merely that one is set (`null` when
GitHub's answer said nothing either way, as above). It can't prove the token may
*dispatch*: on this public repo a read-only token passes too, and only a stale
tick's `"failed"` with status 403 reveals that. Locally it checks whatever token is
in `.dev.vars`, not the deployed secret. It reports what the next tick would do
and deliberately cannot trigger a deploy, so adding a route later wouldn't turn
it into a rebuild button.

To exercise the cron path locally without waiting:

```bash
npx wrangler dev --test-scheduled
# then, in another shell:
curl "http://localhost:8787/__scheduled?cron=*/30+*+*+*+*"
```

## Checking it from the GitHub side

```bash
gh run list --workflow=deploy-pages.yml --limit 10
```

Worker-triggered runs show as `workflow_dispatch`; GitHub's own cron shows as
`schedule`. Seeing both is expected and fine.

## How to tell the pinger is dead

The site's own stale banner won't tell you: GitHub's cron keeps deploying
every ~3 h without the Worker, so the map mostly stays inside its 4.5 h
threshold.

- **GitHub side.** If the published snapshot is more than 2 h old and
  `deploy-pages.yml` has had no `workflow_dispatch` run in the last ~3 h, the
  Worker isn't dispatching:

  ```bash
  gh run list --workflow=deploy-pages.yml --event workflow_dispatch --limit 5
  curl -s https://dmang.com/ca-grid-weather-map/data/manifest.json | jq -r .generated_at
  ```

- **Cloudflare side.** `npx wrangler tail`, or the Worker's Logs and Metrics
  in the dashboard, show thrown errors and invocations with an `exception`
  outcome. A run of `"action":"error"` lines says why.

## What stopped it last time

The first token was a fine-grained PAT left on GitHub's default 30-day
expiry. The Worker dispatched 190 rebuilds between 2026-08-07 and 2026-09-06
02:00Z — exactly 30 days after setup — and nothing for the next 33 days.
Each of the 1,052 stale ticks in between had its dispatch rejected, logged
that with `console.log` and returned normally, so Cloudflare recorded every
one as a success; with no persisted logs, there was nothing to look back at
either. Hence the token check on every tick, the throw, and `[observability]`.

- **Pick a long expiry and note the date** somewhere you'll see it. Renew
  with `npx wrangler secret put GITHUB_TOKEN`; nothing needs redeploying.
- **Scheduled workflows auto-disable.** In a public repo GitHub disables
  scheduled workflows after 60 days without repository activity, and a
  disabled workflow rejects `workflow_dispatch` with a 422, so the Worker
  can't rescue it. With `data/` no longer committed, a quiet two months is
  enough. The Worker reads the workflow's `state` every tick and throws when
  it isn't `active`; re-enable it with `gh workflow enable deploy-pages.yml`
  or from the Actions tab.

## Notes

- If the manifest is unreachable the worker **skips** rather than dispatching,
  and logs the HTTP status as `manifestStatus` so a 404 can be told from a WAF
  challenge (or, when no HTTP answer came back at all, the error as
  `manifestError`). A rebuild can't fix a Pages outage, and retrying every 30 minutes
  would just spam dispatches; the workflow's own cron remains as the fallback.
- Free tier is ample: 48 cron ticks a day against a 100k requests/day limit.
  The token check adds one GitHub API request per tick, far inside its
  5,000/hour limit.
- Changing the cadence means editing `crons` and `MAX_AGE_MINUTES` together —
  the check interval should stay comfortably shorter than the max age.
