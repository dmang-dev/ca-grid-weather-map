/**
 * Keeps the hosted map fresh when GitHub's scheduler doesn't.
 *
 * GitHub Actions' `schedule` trigger is best-effort: over one 39-hour sample
 * it delivered 13 runs against an expected 20, averaging ~3h apart instead of
 * 2h, with one 6.25h gap. Cloudflare's cron triggers are dependable, so this
 * worker watches the published manifest and dispatches a rebuild only when the
 * data has actually gone stale.
 *
 * Checking before dispatching matters: the workflow's own cron is still armed
 * as a backstop, and firing unconditionally would just queue duplicate deploys
 * behind it.
 */

const GITHUB_API = "https://api.github.com";
// Start warning this many days before the token lapses — every tick, so the
// window can't be missed between two looks at the logs.
const TOKEN_WARN_DAYS = 7;

function githubHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    // GitHub rejects API calls without a User-Agent.
    "User-Agent": "cagrid-deploy-pinger",
  };
}

/**
 * Age of the deployed snapshot in minutes (null if it can't be read), with the
 * HTTP status behind it. "Unreadable" alone can't tell a 404 from a WAF
 * challenge from a Pages outage; the status can.
 */
async function manifestAge(env) {
  let res;
  try {
    res = await fetch(env.MANIFEST_URL, {
      headers: { "cache-control": "no-cache" },
      cf: { cacheTtl: 0, cacheEverything: false },
    });
  } catch (err) {
    // No HTTP answer at all (DNS, TLS, a reset connection) is unreadable too,
    // and mustn't reject: that would take this tick's token check down with it.
    return { minutes: null, status: null, error: String(err) };
  }
  if (!res.ok) return { minutes: null, status: res.status };
  // A 200 can still be an HTML interstitial rather than the manifest.
  const manifest = await res.json().catch(() => null);
  const generated = Date.parse(manifest?.generated_at);
  if (!Number.isFinite(generated)) return { minutes: null, status: res.status };
  return { minutes: (Date.now() - generated) / 60000, status: res.status };
}

/**
 * GitHub's github-authentication-token-expiration header reads like
 * "2026-09-06 02:00:00 UTC". That isn't ISO 8601, and Date.parse on anything
 * else is implementation-defined, so normalise the shapes GitHub uses first
 * and only fall back to Date.parse for others. Epoch ms, or NaN.
 */
function parseTokenExpiry(header) {
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s*(UTC|GMT|Z|[+-]\d{2}:?\d{2})?$/i
    .exec(header.trim());
  if (!m) return Date.parse(header);
  const zone = !m[3] || /^(UTC|GMT|Z)$/i.test(m[3])
    ? "Z"
    : m[3].replace(/^([+-]\d{2}):?(\d{2})$/, "$1:$2");
  return Date.parse(`${m[1]}T${m[2]}${zone}`);
}

/**
 * Validates the token against the workflow it has to dispatch — every tick,
 * not just when the snapshot is stale.
 *
 * The first token was a fine-grained PAT on GitHub's default 30-day expiry. It
 * lapsed on 2026-09-06 and the Worker dispatched nothing for 33 days (1,052
 * stale ticks) without anyone noticing: the token was only used once a
 * snapshot went stale, and a rejected dispatch was only logged. This GET
 * catches that case — 401 for an expired or revoked token; 403/404 when the
 * workflow is gone or an SSO/IP-allowlist block stands in the way — and
 * reports the workflow's state, which GitHub flips to "disabled_inactivity"
 * after 60 days without a commit to a public repo. A disabled workflow
 * rejects workflow_dispatch with a 422.
 *
 * What it can't prove is write access. It needs only actions:read, and on a
 * public repo every fine-grained PAT has that, so a token made with Actions
 * read-only (or one that lost the repo) still passes here. That shows up only
 * as "failed" with a 403 on the next stale tick; GitHub has no cheap
 * read-only call that would reveal it sooner.
 *
 * tokenValid is true, false, or null for "GitHub didn't say". Only an auth
 * answer convicts the token; a 5xx, a 429, a rate-limited 403 or an
 * unreachable API says GitHub is unwell, and reading that as a dead token
 * would send someone to rotate one that works — and would suppress the
 * dispatch a stale tick still deserves to attempt.
 */
async function checkToken(env) {
  const token = {
    tokenStatus: null,
    tokenValid: false,
    // No header means the token never expires.
    tokenExpires: null,
    tokenExpiresInDays: null,
    workflowState: null,
  };
  if (!env.GITHUB_TOKEN) return token;
  let res;
  try {
    res = await fetch(
      `${GITHUB_API}/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}`,
      { headers: githubHeaders(env) },
    );
  } catch (err) {
    return { ...token, tokenValid: null, tokenError: String(err) };
  }
  token.tokenStatus = res.status;
  // GitHub answers an exhausted rate limit with 403 or 429 and
  // x-ratelimit-remaining: 0; the limit is per user, so other use of the
  // same account can spend it.
  const rateLimited = res.headers.get("x-ratelimit-remaining") === "0";
  token.tokenValid = res.ok
    ? true
    : res.status === 401 || res.status === 404 || (res.status === 403 && !rateLimited)
      ? false
      : null;
  const header = res.headers.get("github-authentication-token-expiration");
  if (header) {
    const expires = parseTokenExpiry(header);
    // Keep the raw header if it won't parse, so a format change shows in the log.
    token.tokenExpires = Number.isFinite(expires) ? new Date(expires).toISOString() : header;
    if (Number.isFinite(expires)) {
      token.tokenExpiresInDays = Math.round(((expires - Date.now()) / 864e5) * 10) / 10;
    }
  }
  if (!res.ok) {
    token.tokenError = await res.text().catch(String);
    return token;
  }
  // The body is held to the same rule. A 200 cut off mid-read, or one that
  // isn't GitHub's workflow JSON (a proxy's HTML page, say), says nothing
  // either way — and letting it throw would cost a stale tick its dispatch.
  const workflow = await res.json().catch(() => null);
  if (typeof workflow?.state !== "string") {
    return { ...token, tokenValid: null, tokenError: "workflow response unreadable" };
  }
  token.workflowState = workflow.state;
  return token;
}

async function dispatchWorkflow(env) {
  const url =
    `${GITHUB_API}/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: { ...githubHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: env.REF || "main" }),
  });
  // A successful workflow_dispatch returns 204 with an empty body.
  if (res.status === 204) return { dispatched: true, status: 204 };
  return { dispatched: false, status: res.status, error: await res.text().catch(String) };
}

/**
 * What this tick should do — "skipped", "error" or "dispatch" — without doing
 * it. Read-only, so the status endpoint shares it and can't drift from what
 * the cron actually does.
 */
async function assess(env) {
  const maxAge = Number(env.MAX_AGE_MINUTES || 110);
  const [manifest, token] = await Promise.all([manifestAge(env), checkToken(env)]);
  const facts = {
    ageMinutes: manifest.minutes === null ? null : Math.round(manifest.minutes),
    manifestStatus: manifest.status,
    // Only set when there was no HTTP status to report; JSON drops it otherwise.
    manifestError: manifest.error,
    maxAge,
    ...token,
  };

  // Token and workflow come before freshness on purpose: a dead token is an
  // error on every tick, not only on the ticks that happen to need it.
  if (!env.GITHUB_TOKEN) {
    return { action: "error", reason: "GITHUB_TOKEN secret is not set", ...facts };
  }
  if (token.tokenValid === false) {
    return {
      action: "error",
      reason: token.tokenStatus === 401
        ? "GitHub rejected GITHUB_TOKEN (401): expired or revoked"
        : `GITHUB_TOKEN can't read ${env.WORKFLOW} (HTTP ${token.tokenStatus})`,
      ...facts,
    };
  }
  // tokenValid null: the check was inconclusive, so decide on freshness alone,
  // as before the check existed. If the token really is dead, a stale tick's
  // dispatch fails and throws as "failed".
  if (token.tokenValid && token.workflowState !== "active") {
    return {
      action: "error",
      reason: `${env.WORKFLOW} is ${token.workflowState}; GitHub rejects dispatches to it`,
      ...facts,
    };
  }
  if (manifest.minutes === null) {
    // Pages itself is unreachable or serving something unparseable. A rebuild
    // won't fix that, and retrying every cron tick would just spam dispatches,
    // so leave it to the workflow's own schedule and report the fact.
    return { action: "skipped", reason: "manifest unreadable", ...facts };
  }
  if (manifest.minutes <= maxAge) {
    return { action: "skipped", reason: "still fresh", ...facts };
  }
  return { action: "dispatch", ...facts };
}

export async function check(env) {
  const plan = await assess(env);
  if (plan.action !== "dispatch") return plan;
  const result = await dispatchWorkflow(env);
  return { ...plan, action: result.dispatched ? "dispatched" : "failed", ...result };
}

export default {
  async scheduled(controller, env, ctx) {
    let r;
    try {
      r = { cron: controller.cron, ...(await check(env)) };
    } catch (err) {
      // check() turns every failure it expects into a result. Anything else —
      // a dispatch POST that never got an answer, say — is still logged and
      // still declines the retry (see below) before it propagates.
      console.error(JSON.stringify({ cron: controller.cron, action: "exception", error: String(err) }));
      controller.noRetry?.();
      throw err;
    }

    if (r.tokenExpiresInDays !== null && r.tokenExpiresInDays <= TOKEN_WARN_DAYS) {
      console.warn(JSON.stringify({
        warning: `GITHUB_TOKEN expires ${r.tokenExpires}, in ${r.tokenExpiresInDays} days; ` +
          "renew it with `npx wrangler secret put GITHUB_TOKEN`",
        tokenExpires: r.tokenExpires,
      }));
    } else if (r.tokenExpires && r.tokenExpiresInDays === null) {
      console.warn(JSON.stringify({ warning: "unparseable token expiry header", tokenExpires: r.tokenExpires }));
    }
    if (r.tokenValid === null) {
      console.warn(JSON.stringify({
        warning: "token check inconclusive: GitHub " +
          (!r.tokenStatus ? `unreachable (${r.tokenError})`
            : r.tokenStatus < 300 ? `returned HTTP ${r.tokenStatus} with an unreadable body`
            : `returned HTTP ${r.tokenStatus}`) +
          "; token and workflow state unknown this tick",
        tokenStatus: r.tokenStatus,
      }));
    }

    if (r.action === "skipped" || r.action === "dispatched") {
      console.log(JSON.stringify(r));
      return;
    }
    console.error(JSON.stringify(r));
    // Throw, don't just log. Failures used to end in console.log and a
    // resolved promise, so Cloudflare recorded all 1,052 ticks of the
    // Sept 2026 outage as successes. A rejected handler marks the invocation
    // as an exception in the Worker's metrics, its past cron events and
    // Workers Logs; check() is awaited here rather than handed to
    // ctx.waitUntil so the throw lands on the handler's own promise.
    // noRetry() keeps that from adding dispatch attempts: the next tick,
    // 30 minutes on, is the retry.
    controller.noRetry?.();
    throw new Error(`deploy pinger ${r.action}: ${r.reason ?? `dispatch returned HTTP ${r.status}`}`);
  },

  // Read-only status endpoint — reports what the next cron tick would do,
  // including whether the token still works. Deliberately does NOT dispatch:
  // it calls assess(), never check(), so the public URL can't be used to
  // trigger deploys. Use `npx wrangler dev --test-scheduled` to exercise the
  // cron path.
  async fetch(request, env) {
    const plan = await assess(env);
    return Response.json({
      repo: env.REPO,
      workflow: env.WORKFLOW,
      manifestUrl: env.MANIFEST_URL,
      nextTick: plan.action,
      reason: plan.reason ?? null,
      ageMinutes: plan.ageMinutes,
      manifestStatus: plan.manifestStatus,
      manifestError: plan.manifestError,
      maxAgeMinutes: plan.maxAge,
      wouldDispatch: plan.action === "dispatch",
      tokenConfigured: Boolean(env.GITHUB_TOKEN),
      tokenValid: plan.tokenValid,
      tokenStatus: plan.tokenStatus,
      tokenExpires: plan.tokenExpires,
      tokenExpiresInDays: plan.tokenExpiresInDays,
      workflowState: plan.workflowState,
    });
  },
};
