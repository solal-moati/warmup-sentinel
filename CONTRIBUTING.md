# Keep personal information out of commits

Before committing, copy your GitHub `noreply` address from
[Settings → Emails](https://github.com/settings/emails) and configure it
for this checkout:

```sh
git config --local user.email "YOUR_GITHUB_NOREPLY_ADDRESS"
git config --local user.useConfigOnly true
git config --local core.hooksPath .githooks
```

The local pre-commit hook rejects author or committer addresses that are
not GitHub `noreply` addresses. It must be enabled in each clone; it does
not run for edits made on GitHub's website. For web edits and other
checkouts, enable **Keep my email addresses private** and **Block
command line pushes that expose my email** in your GitHub email settings.

These settings protect future commits. Existing commits retain their
original metadata unless their history is explicitly rewritten.

Do not add real `.env` files, API keys, webhook URLs or generated `data/`
to public contributions. Use a separate private copy for live monitoring.
