# Run your first open pipeline review

This project reads HubSpot or a mapped CRM-neutral JSON export, flags open deals that need attention and prepares a local
recap with links to the deals when your HubSpot portal ID is configured. It does not
modify the CRM. Follow-ups and meeting briefs are proposals for you to review.

## Start with a mapped export or a fictional example

With Python 3.9 or later, no installed dependencies or API keys are needed for this path:

```bash
python3 snapshot.py --input examples/neutral-snapshot.json --output output/neutral-analysis.json
python3 preview_recap.py --input output/neutral-analysis.json --output output/neutral-recap.json --timezone Europe/Paris
```

Open `output/neutral-recap.html`. These three cases are fictional, dated test fixtures.
They illustrate an overdue commitment, an incoming postponement and an already planned
meeting. They are not customer results. To use another CRM, map your authorized export
to [the documented contract](docs/snapshot-contract.md). The importer does not connect
to Attio or Folk itself. Keep real inputs and outputs outside repositories.

## Configure a live HubSpot reading

Use Python 3.9 or later. From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
cp config.example.json config.json
```

Add your HubSpot token to `.env`. Use a token with read access to the objects you want
to analyze. Run `python3 probe.py --list-stages`, then set the open stage IDs and pipeline
ID in `config.json`. Set the next-step fields and thresholds to match your sales cycle.
Stage names alone do not determine whether a deal is healthy or stalled.

## Use your existing assistant

Give `assistant-review.txt` to the coding assistant you already use, with access to this
folder. No separate model API key is required for this path. Your assistant still runs
under its own subscription, usage limits and data processing terms.

For HubSpot, collect a private review snapshot for all open deals of the selected owner:

```bash
python3 analyze.py --owner-email rep@example.com --export-review \
  --output-dir ~/.local/share/open-pipeline-agent/private-run --label first
```

Your assistant reads `analysis_first.json` and its metadata, then writes
`proposals.json` in that same private folder according to `assistant-review.txt`.
The export includes CRM text: keep it outside repositories and shared folders.
Only use an assistant approved to process that data.

For a neutral export, the analysis and metadata produced by `snapshot.py` work with
the same assistant and validation steps below. Its capture time is preserved. The
fixed-date fictional fixture is intentionally unsuitable for fresh assistant review.

Validate and render:

```bash
python3 review.py \
  --input ~/.local/share/open-pipeline-agent/private-run/analysis_first.json \
  --proposals ~/.local/share/open-pipeline-agent/private-run/proposals.json \
  --output ~/.local/share/open-pipeline-agent/private-run/reviewed.json
python3 preview_recap.py \
  --input ~/.local/share/open-pipeline-agent/private-run/reviewed.json \
  --timezone Europe/Paris \
  --output ~/.local/share/open-pipeline-agent/private-run/recap.json
```

Open `recap.html` in that private folder. It is an inspection view of the Slack-format
recap. Nothing is posted to Slack. Every deal stays visible; long recaps are split into
messages. Wrong quotes, conflicting plans and unsupported dates become review items.
An export older than 24 hours must be collected again before importing proposals.
This prevents reuse of an old snapshot; it does not guarantee the CRM has not changed
in the meantime. Recheck a deal before sending a message.

## Optional direct model API

`analyze.py --generate` uses Anthropic when `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`
are configured. It is a separate paid API call. The same proposal checks apply.

## Current boundaries

- HubSpot connector and neutral JSON importer implemented; native Attio/Folk adapters are not included.
- Direct activities by default; optional indirect HubSpot discovery with explicit
  confirmation for ambiguous links. This new route has offline tests, not live validation.
- No drafts saved in a mailbox, no automatic email or Slack send.
- No recurring runner installed. Days, hours and timezone can be configured, but
  `scheduling.py` currently calculates occurrences only.
- Model proposals require human review. Exact citations do not establish that the
  interpretation is correct. Partial evidence is stated in the recap.

Try `python3 demo.py` without a CRM connection, or run
`python3 -m unittest discover -s tests` for offline checks.

To adapt the rules or contribute a connector, start with [CONTRIBUTING](CONTRIBUTING.md)
and the [shared guidelines](docs/guidelines.md).

## Review memory and bilateral commitments

Run `python3 community_demo.py` for three fictional review cycles. Open
`output/community-demo/cycle-1.html`, then cycle 2 and 3. All deals remain available;
previously reviewed ones are collapsed until new context or the agreed review date.
The commitment extraction in this demonstration is simulated.

The [foundations guide](docs/community-foundations.md) documents optional context
discovery, cited commitments, local review decisions and their limitations.
The order is: collect, import commitments, validate proposals, apply local memory,
render the recap. Each enrichment uses separate private input and output files.
Local memory needs macOS or Linux; it does not write to your CRM.
