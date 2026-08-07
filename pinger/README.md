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

## A note on `npm audit`

`npm audit` reports a few advisories against `undici`, reached via
`wrangler → miniflare → undici`. Two things to know before chasing them:

- **Nothing vulnerable ships.** miniflare is Wrangler's *local* dev sandbox.
  The deployed bundle is ~3 KiB of this worker and contains no `undici` at
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
- Set an expiry you're happy to rotate on; the worker starts failing loudly
  in `wrangler tail` when it lapses.

**2. Install and authenticate** (Wrangler 4; needs Node 20+)

```bash
cd pinger
npm install
npx wrangler login
```

**3. Store the token as a secret**

```bash
npx wrangler secret put GITHUB_TOKEN
```

Paste the token at the prompt. It is write-only from then on — not readable
from the dashboard, not in `wrangler.toml`, not in git.

**4. Deploy**

```bash
npx wrangler deploy
```

## Verifying

```bash
npx wrangler tail          # live logs; one line per cron tick
```

Each tick logs its decision:

```json
{"cron":"*/30 * * * *","action":"skipped","reason":"still fresh","ageMinutes":47,"maxAge":110}
{"cron":"*/30 * * * *","action":"dispatched","ageMinutes":118,"maxAge":110,"status":204}
```

The worker's URL is a **read-only** status endpoint — it reports what the next
tick would do and deliberately cannot trigger a deploy, so the public URL is
not a rebuild button:

```json
{"ageMinutes":47,"maxAgeMinutes":110,"wouldDispatch":false,"tokenConfigured":true}
```

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

## Notes

- If the manifest is unreachable the worker **skips** rather than dispatching.
  A rebuild can't fix a Pages outage, and retrying every 30 minutes would just
  spam dispatches; the workflow's own cron remains as the fallback.
- Free tier is ample: 48 cron ticks a day against a 100k requests/day limit.
- Changing the cadence means editing `crons` and `MAX_AGE_MINUTES` together —
  the check interval should stay comfortably shorter than the max age.
