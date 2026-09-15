# warmup-sentinel

An autonomous agent that watches the deliverability of a cold-email setup
running on [lemlist](https://www.lemlist.com) + lemwarm, takes the protective
decisions for you, and only speaks up to tell you what to do. Which is,
almost every morning: "nothing needed from you".

Born from a real case: lemwarm's blacklist check showed 100/100 while all 6
of our sending domains sat on the SURBL domain blocklist, and 3 mailboxes had
silently stopped sending new warmup emails for up to 10 days while still
showing as active in the UI. This agent is how we saw it, and how we make
sure we never miss it again.

## What it does, every day, on its own

- **Reads every sending mailbox** (lemwarm score, inbox placement, whether
  warmup actually runs). The API keeps no history, so a day not collected is
  lost: hence the append-only `data/history.csv`.
- **Detects what the dashboard does not show**: a stuck outgoing warmup
  (`warmNextEmailAt` frozen in the past while the mailbox displays as
  active), and listings on the **domain** blocklists (SURBL, Spamhaus DBL,
  URIBL), which lemwarm's blacklist check did not flag in our case.
- **Ranks mailboxes** (healthy / recovering / resting / quarantined) over a
  rolling 7-day window, and derives your **real send capacity**: how many new
  leads can go out today, follow-ups included (one lead ≈ 3.5 emails over its
  sequence).
- **Forecasts the week**: reads every running campaign's steps, delays and
  real sends, then shows, day by day, the follow-ups already committed per
  mailbox and the largest steady number of new leads you can push without
  exceeding any mailbox's cap, follow-ups included. The delay rule (lemlist
  counts business days) is re-checked on your own past sends every run;
  under 85% accuracy the plan is withheld rather than guessed.
- **Decides**: leads whose address bounces or is undeliverable are paused
  automatically (at most 10 per run, never removed, never your test leads)
  and logged to `data/paused-leads.json`. A "risky" verdict alone never
  pauses anyone: the mail was accepted, the lead may still answer.
- **Rings on changes, not on known states**: a stuck warmup or a blocklist
  listing rings once when it appears (on the first run, once for whatever is
  already listed), and again when it clears. Breakages (disconnected mailbox,
  stale collection, API error) ring until fixed. On Mondays, a six-line
  week-in-review.
- **Posts a morning ping** to your chat (Google Chat or Slack, plain incoming
  webhook): today's number, the mailbox states, and "nothing needed from you"
  or the one precise action.

## Setup (15 minutes)

1. **Fork or clone** this repo into a **private** repository: the workflow
   commits `data/` (your mailbox addresses, their scores, the paused leads)
   into it. Then in the repo's GitHub secrets
   (`Settings → Secrets and variables → Actions`):
   - `LEMLIST_API_KEY` — your lemlist API key;
   - `CHAT_WEBHOOK` — a Google Chat or Slack incoming-webhook URL
     (optional, for the morning ping).
2. In the fork's **Actions** tab, enable the two workflows (GitHub keeps
   scheduled workflows off on a fresh fork). They then run daily (collection
   + guard) and weekday mornings (capacity ping). The first run seeds `data/`.
3. Locally: copy `.env.example` to `.env`, then
   ```bash
   cd scripts
   python3 sentinel_collect.py --dry-run   # reads the API, writes nothing
   python3 sentinel_report.py              # builds data/report.md
   python3 sentinel_selftest.py            # 29 tests, one second
   python3 capacity_ping.py --dry-run      # the morning ping, without posting
   python3 lead_guard.py                   # the guard, dry-run
   python3 sentinel_alerts.py              # the lemlist alerts, dry-run
   python3 send_forecast.py                # committed follow-ups and the week's plan
   ```
   Only dependency: `pip install requests`.

Optional variables: `SITE_DOMAINS` (extra domains to watch on the
blocklists, your website for instance) and `TEST_LEAD_DOMAINS` (domains of
your internal test leads, never paused) go in `.env` locally and in the
repo's Actions variables for CI; `ALERT_EMAIL` (recipient of the lemlist
alerts) is only needed locally, when you create the alerts with
`sentinel_alerts.py --commit --only <rule>`.

Operator rules are ours, not provider limits, and they are overridable the
same way (see `.env.example`): `BOX_DAILY_CAP` (50 emails per mailbox per
day, warmup included), `ACTIVE_MIN` / `RECOVERING_MIN` /
`QUARANTINE_INBOX_BELOW` (84 / 80 / 70), `EMAILS_PER_LEAD` (3.5),
`TARGET_NEW_LEADS` (80) and `PAUSE_CAP` (10). Four Microsoft mailboxes with a
cap of 30? Two variables, no code change.

## What the agent refuses to do

- **One automatic write**: the capped pausing of dead addresses. The only
  other write is creating lemlist alerts, run by hand, one rule at a time,
  never duplicated. Everything else is read-only by construction
  (`WarmupClient` rejects any verb other than GET before a single network
  call), and a lead is **never** removed from a campaign: it would restart
  from step 1.
- It never starts a campaign, never touches warmup settings, never edits a
  sequence.
- A run goes red on a change or a breakage; a known stall or listing does
  not ring again.

## What it does not measure (yet)

- **It reads lemwarm's thermometer, not your real sends.** The lemwarm score
  follows warmup volume: a mailbox whose outgoing warmup stalls loses score
  day after day even if its campaign emails still land. Bounce rate and
  reply rate per mailbox, read from the campaign activities, are the next
  signal to add.
- **Capacity is a cruise-speed rule, not the room left today.** It does not
  read the follow-ups already scheduled: the morning after a large push, the
  number is optimistic.
- **A domain blocklist listing is a signal, not a measured impact.** SURBL,
  DBL and URIBL feed SpamAssassin-style filters; Gmail and Microsoft 365 run
  their own reputation systems. The agent reports the listing, it does not
  claim to know how much of your placement it costs.

## API facts learned in production (undocumented)

- A nonexistent path answers **HTTP 200 + the app's HTML shell** instead of
  a 404: validate the content, never the status.
- Observed: once the DNS, mailbox-age and sent-volume components are maxed
  out, the lemwarm score tracks roughly 70 + (warmups landed in inbox ÷ 10).
  It follows warmup **volume** more than real health, hence the ranking's
  veto on actual placement.
- A `warmNextEmailAt` frozen in the past = outgoing warmup stopped, even
  while `lastWarmAt` keeps moving and the mailbox shows as active. lemlist
  support confirmed such mailboxes had been paused by lemwarm itself.
- lemwarm sends **no warmup on weekends** (confirmed by lemlist support): the
  freshness checks exclude Saturday and Sunday, otherwise every Monday rings
  falsely.
- The public API cannot set a campaign's senders (the PATCH answers 200 and
  changes nothing): `sender_check.py` verifies them instead, before any push.
- Inbox placement swung 5 to 12 points from one day to the next on the same
  mailbox: every decision runs on a 7-day window, never on a single day.

## Hand your agent the first audit

No install needed for a first diagnosis: paste this to an AI agent that
holds your lemlist API key. The prompt itself forbids every write.

```text
Audit our lemlist sending setup. Read-only: do not send messages, do not
change campaigns, mailboxes, warmup settings or leads. Keep credentials
private.

1. Enumerate every sending mailbox (GET /team, then GET /users/{id} for each
   user id), including mailboxes in no campaign. Validate response CONTENT,
   not the HTTP status: nonexistent paths answer 200 with an HTML page.
2. For each mailbox, read GET /lemwarm/{mailboxId}/settings and report the
   deliverability score, inbox placement, lastWarmAt and warmNextEmailAt.
   Flag any mailbox whose warmNextEmailAt is more than 24 business hours in
   the past (weekends excluded): its outgoing warmup is stuck, even if it
   shows as active.
3. Check every sending domain, the tracking domain and our website against
   the domain blocklists over DNS: multi.surbl.org, dbl.spamhaus.org and
   multi.uribl.com. Only trust a list when its test entry answers and a
   clean control (example.com) returns NXDOMAIN.
4. List every campaign lead that is inProgress with an emailStatus of risky
   or undeliverable, or whose last event is a bounce. Recommend pausing
   them, never removing them: a removed lead restarts its sequence.
5. Grade each mailbox: healthy = score 84+, recovering = 80 to 83, resting =
   below 80, or inbox placement under 70% (this overrides the score). Then
   compute today's capacity: (50 minus the mailbox's warmEmailMax) campaign
   emails per healthy mailbox, half that per recovering mailbox, none for
   resting ones, and divide the total by 3.5 emails per lead. These are our
   operating rules, not provider limits: adjust them to yours.
6. Return a short decision list with the evidence behind each item, and the
   data you could not get. Recommend no setting change without an owner's
   approval.
```

## License

MIT. Built by [Pipelab](https://pipelab.run) — we install and operate the
full version (address verification before every send, sender rotation,
campaign pacing) for our clients.
