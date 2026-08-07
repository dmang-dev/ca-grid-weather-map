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

/** Age of the deployed snapshot in minutes, or null if it can't be read. */
async function manifestAgeMinutes(env) {
  const res = await fetch(env.MANIFEST_URL, {
    headers: { "cache-control": "no-cache" },
    cf: { cacheTtl: 0, cacheEverything: false },
  });
  if (!res.ok) return null;
  const manifest = await res.json();
  const generated = Date.parse(manifest.generated_at);
  if (!Number.isFinite(generated)) return null;
  return (Date.now() - generated) / 60000;
}

async function dispatchWorkflow(env) {
  const url =
    `${GITHUB_API}/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      // GitHub rejects API calls without a User-Agent.
      "User-Agent": "cagrid-deploy-pinger",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.REF || "main" }),
  });
  // A successful workflow_dispatch returns 204 with an empty body.
  if (res.status === 204) return { dispatched: true, status: 204 };
  return { dispatched: false, status: res.status, error: await res.text() };
}

export async function check(env) {
  if (!env.GITHUB_TOKEN) {
    return { action: "error", reason: "GITHUB_TOKEN secret is not set" };
  }
  const age = await manifestAgeMinutes(env);
  const maxAge = Number(env.MAX_AGE_MINUTES || 110);

  if (age === null) {
    // Pages itself is unreachable or serving something unparseable. A rebuild
    // won't fix that, and retrying every cron tick would just spam dispatches,
    // so leave it to the workflow's own schedule and report the fact.
    return { action: "skipped", reason: "manifest unreadable", ageMinutes: null };
  }
  if (age <= maxAge) {
    return { action: "skipped", reason: "still fresh", ageMinutes: Math.round(age), maxAge };
  }
  const result = await dispatchWorkflow(env);
  return { action: result.dispatched ? "dispatched" : "failed", ageMinutes: Math.round(age), maxAge, ...result };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      check(env).then((r) => console.log(JSON.stringify({ cron: event.cron, ...r })))
    );
  },

  // Read-only status endpoint — reports what the next cron tick would do.
  // Deliberately does NOT dispatch, so the public URL can't be used to trigger
  // deploys. Use `npx wrangler dev --test-scheduled` to exercise the cron path.
  async fetch(request, env) {
    const age = await manifestAgeMinutes(env);
    const maxAge = Number(env.MAX_AGE_MINUTES || 110);
    return Response.json({
      repo: env.REPO,
      workflow: env.WORKFLOW,
      manifestUrl: env.MANIFEST_URL,
      ageMinutes: age === null ? null : Math.round(age),
      maxAgeMinutes: maxAge,
      wouldDispatch: age !== null && age > maxAge,
      tokenConfigured: Boolean(env.GITHUB_TOKEN),
    });
  },
};
