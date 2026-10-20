# Build on Open Pipeline Agent

You do not have to code to contribute. A sales workflow that the review misunderstands
is useful input. The goal is to prepare a useful next move from recorded facts while
respecting the way each team works.

## Start with a sales case

Use the [six-question template](docs/business-case-template.md) or the repository's
**Sales case** issue form. Executable cases named
`examples/community-case-*.json` are discovered automatically by the offline tests.

Describe the situation, the facts the CRM contains, what the review returned and what
you would have wanted instead. For example: "The buyer asked us to wait until their
budget review. There has been no email since then. The recap should preserve the wait,
not recommend another follow-up."

Use fictional names, IDs, domains and rewritten messages. Do not upload a real CRM
export, screenshot, token, recording or client document in an issue or pull request.
If you cannot safely share the data, describe the behavior without the data.

## Four useful contributions

1. A sales case: a false alarm, missing context or an important exception to a rule.
2. A configuration: how stages and thresholds fit your cycle, with fictional examples.
3. An adapter: read your CRM and produce the [snapshot contract](docs/snapshot-contract.md).
   Attio and Folk are welcome; no native connector for either is included today.
4. A preparation: a better cited follow-up or meeting brief, still reviewed by the rep.

## Development

Python 3.9 or later. Offline tests and snapshot review require only the standard library.
Install `requirements.txt` for the HubSpot and optional model API paths.

```bash
python3 -m unittest discover -s tests -v
python3 snapshot.py --input examples/neutral-snapshot.json --output output/neutral-analysis.json
python3 preview_recap.py --input output/neutral-analysis.json --output output/neutral-recap.json --timezone Europe/Paris
```

Add a regression case for a changed decision or a new adapter. Test missing data and
conflicting plans, not just the happy path. Label synthetic tests as synthetic: they
do not establish live API compatibility or sales outcomes.

The existing signal engine uses HubSpot-style fields internally. `snapshot.py` maps
neutral fields to those internals so contributors do not need to imitate HubSpot in
their exports. It deliberately reuses the existing decision and validation code.

## Pull request checklist

- Explain the sales problem and show the changed output on a fictional case.
- Preserve all selected open deals; no arbitrary cap, silent omissions or hidden errors.
- State what your connector can read, its association coverage and how pagination ends.
- Preserve timestamps, source IDs, existing plans and contact restrictions.
- Keep reads separate from sends, CRM writes and recurring runs. New side effects
  must be explicit, optional and documented. Current code never executes them.
- Run the offline test suite. Include no customer data or credentials.
- Update the compatibility table when a connector is actually implemented and tested.

Use, modify and redistribute the code under [MIT](LICENSE). Contributions are provided
under the same license. Only contribute code and examples you have the right to share.
