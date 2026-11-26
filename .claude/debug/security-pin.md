## Security channel: automatic pin was not wired (tk-c2d005, 2026-11-25)

Symptom: security-channel.yml listed every candidate of `apt-get -s upgrade`
and rebuilt at once; security-care.sh existed but nothing called it; no
advisory gate, no CISA KEV feed.

Proved (code): the detect job wrote `upgrades.txt` straight from
security-detect.sh; no step referenced security-care.sh.

Facts used (OSV, queried live): the Debian ecosystem answers with many
`DEBIAN-CVE-*` tracker records for the adopted version, so holding on any
id would always block; only non-tracker advisories hold.

Bug found by the new tests while writing the fix: an awk `NR == FNR`
two-file idiom treats the second file as the first when the first is empty
(no fixed CVE, nothing adopted), which dropped every upgrade
(tests/test_security_adopt.py: 2 failures before the fix, 6 pass after).

Fix: scripts/security-osv.sh, security-urgent.sh, security-advisory.sh,
security-adopt.sh; the detect job runs security-adopt.sh with the care ledger
in the Actions cache; report.md goes into the release notes.
Open until a real run: the first scheduled run of the pushed workflow.
