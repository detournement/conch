#!/bin/sh
# check-secrets.sh — local secret scan for conch.
#
# Scans the full reachable git history and the working tree for credential
# material. Uses gitleaks (honoring the repo's .gitleaks.toml allowlist)
# when installed — `brew install gitleaks` — and falls back to a
# conservative git-grep battery over the checked-out tree otherwise.
#
# Usage:
#   scripts/check-secrets.sh
#
# Optional pre-push hook (NOT installed automatically — opt in with):
#   ln -s ../../scripts/check-secrets.sh .git/hooks/pre-push
#
# Exit status: 0 clean, 1 findings, 2 could not scan.
set -u

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "check-secrets: not inside a git repository" >&2
    exit 2
}
cd "$root" || exit 2

fail=0

if command -v gitleaks >/dev/null 2>&1; then
    # Committed content, every ref.
    gitleaks git --no-banner --redact --config .gitleaks.toml \
        --log-opts=--all . || fail=1
    # Working tree, including untracked files.
    gitleaks dir --no-banner --redact --config .gitleaks.toml . || fail=1
else
    echo "check-secrets: gitleaks not installed (brew install gitleaks);" \
        "using the git-grep fallback (tracked files only, no history)" >&2
    battery='sk-ant-[A-Za-z0-9_-]{10,}|sk-proj-[A-Za-z0-9_-]{10,}'
    battery="$battery|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
    battery="$battery|xox[baprsoe]-[0-9A-Za-z-]{5,}"
    battery="$battery|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    battery="$battery|glpat-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}"
    battery="$battery|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    battery="$battery|ATATT3[A-Za-z0-9_=-]{20,}"
    if git grep -nE "$battery" -- ':!scripts/check-secrets.sh'; then
        fail=1
    fi
    # Capitol A2A bearers, minus the synthetic test fixtures.
    if git grep -nE 'cap_a2a_[A-Za-z0-9_-]{4,}' -- ':!scripts/check-secrets.sh' ':!.gitleaks.toml' |
        grep -vE 'cap_a2a_(TESTTOKENxx0123456789abcdef|WRONG|LOCAL|CUSTOM|MINTEDBEARER0001|ROTATEDBEARER002)'; then
        fail=1
    fi
fi

if [ "$fail" -ne 0 ]; then
    echo "check-secrets: findings above. If a finding is a synthetic" \
        "fixture, extend the allowlist in .gitleaks.toml; if it is real," \
        "rotate the credential first, then remove it." >&2
fi
exit "$fail"
