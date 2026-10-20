# Data stays in your private working copy

This repository is a public template. Keep customer records, CRM exports,
credentials, generated reports, proposals and local review decisions out of it.
Use a private repository or a private directory for real operations.

## Pipeline agent

- Run the fictional demonstration before connecting a CRM.
- Use a HubSpot token restricted to the read permissions the connector needs.
- Store `.env`, configuration, exports and local memory outside this public checkout.
- Optional model calls or hosted assistants can transmit the context they read.
  Use only a provider/account approved to process your organization's CRM data.
- Source citations are consistency checks, not a guarantee that an interpretation is correct.
- The delivered pipeline agent does not send messages, write to the CRM or install a scheduler.

## Email agent

Its workflows commit monitoring output in private operating copies. Their existing
public-repository guard must remain enabled. Do not make a live operating copy public.

## Before a public contribution

Use the GitHub noreply identity described in `CONTRIBUTING.md`. Enable `.githooks`.
Review the actual staged files. Run the offline tests and the Gitleaks scan.
Publish only fictional examples. A secret scanner does not detect every kind of
personal, commercial or confidential information.

If you find an exposed secret, notify the maintainer privately with its location,
not its value. Revoke or rotate it at the provider. Deleting the current file does
not remove previous Git revisions, forks, caches or existing clones.

Do not post sensitive evidence in a public issue. Use fictional examples when
reporting ordinary behavior bugs.
