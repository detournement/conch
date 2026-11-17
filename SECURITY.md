# Security

## Secret hygiene

This is a public repository. Never commit credential material: API keys,
bearer tokens, private keys, `.env` files, or state/log/database files that
can embed live identifiers. Configuration references credentials by
environment-variable name (`api_key_env = ANTHROPIC_API_KEY`) — keep it that
way. Runtime state (`*.log`, `*.db`, `tasks.json`, `memory.json`, `*.bak`,
`config.pre-*`) is gitignored; do not force-add it.

Test fixtures that look like credentials must be obviously synthetic
(`cap_a2a_TESTTOKENxx0123456789abcdef`, all-zero UUIDs) and allowlisted in
`.gitleaks.toml` in the same commit that introduces them. Never allowlist a
value that has ever been a real credential.

## Scanning

- **CI:** `.github/workflows/secret-scan.yml` runs
  [gitleaks](https://github.com/gitleaks/gitleaks) with the repo's
  `.gitleaks.toml` over the full fetched history on every push and pull
  request, and fails on findings.
- **Locally:** run `scripts/check-secrets.sh` (uses gitleaks when installed,
  `brew install gitleaks`; otherwise a git-grep fallback). To run it
  automatically before every push, opt in with:

  ```sh
  ln -s ../../scripts/check-secrets.sh .git/hooks/pre-push
  ```

## If a real secret lands

1. **Rotate the credential immediately.** Assume it is compromised the
   moment it reaches the public remote; removing it from git later does not
   un-leak it.
2. Remove it from the tip of every branch that carries it.
3. Decide whether to scrub history (`git filter-repo` / BFG + force-push);
   coordinate with everyone who has clones, since all branches must be
   re-based onto the rewritten history.
