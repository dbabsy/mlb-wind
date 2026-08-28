# Triggering the build

GitHub's own scheduler does not work reliably on this repo. Measured over
several days it delivered roughly 15% of requested runs, then stopped
entirely — 47 hours with none, while the workflow was active, the cron valid,
and Actions healthy. Reducing the request rate did not help.

`workflow_dispatch` has never once failed. So the build is triggered from
outside GitHub, and the `schedule:` block in `build.yml` is left in place only
as a free backstop for whenever GitHub's scheduler decides to work.

## The endpoint

```
POST https://api.github.com/repos/dbabsy/mlb-wind/actions/workflows/build.yml/dispatches
```

| Header | Value |
|---|---|
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer YOUR_TOKEN` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

Body:

```json
{"ref": "main"}
```

A success is **HTTP 204 No Content** with an empty body. Anything else means
the trigger did not fire — 401 is a bad or expired token, 404 usually means the
token lacks Actions permission on this repo rather than that the URL is wrong.

## The token

A **fine-grained personal access token**, scoped as narrowly as this job needs:

- Repository access: **only** `dbabsy/mlb-wind`
- Permission: **Actions → Read and write** (nothing else)
- Expiry: set a reminder — the build goes quiet when it lapses

That token can start workflows on this one repo and nothing else. It cannot
read other repositories, push code, or touch the account.

## Schedule

Five triggers a day, in `America/Chicago`, as one crontab expression:

```
0 7,14,16,17,22 * * *
```

| Central | Why |
|---|---|
| 7:00am | Roll to today's slate; score last night's games |
| 2:00pm | First lineups appearing |
| 4:00pm | Lineups filling in |
| 5:00pm | Most lineups posted before the evening games |
| 10:00pm | Score the early finals |

All five sit on the hour deliberately. cron-job.org builds its schedule as a
cross product of the minute and hour lists, so mixing `:00` and `:30` times
would fire at every combination of both — ten runs a day instead of five.

Set the job's timezone to `America/Chicago` and the times hold through the
daylight-saving change on their own. Using UTC instead would need editing
twice a year.

## Checking it works

```bash
gh api "repos/dbabsy/mlb-wind/actions/runs?per_page=5" \
  -q '.workflow_runs[] | "\(.created_at) \(.event) \(.conclusion)"'
```

Rows with event `workflow_dispatch` at the times above mean the trigger is
firing. The `Updated` stamp on any page shows the same thing without a
terminal, and turns amber when it is more than four hours old.
