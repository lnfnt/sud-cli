# Security policy

- This tool works with openly published court/enforcement data only.
- Captcha challenges are solved exclusively by a human (no OCR, no solver
services) and every assisted search is recorded with provenance metadata.
- Egress follows a fail-closed proxy policy; hosts returning rate-limit
answers go into cooldown automatically.
- Report vulnerabilities to the project maintainer (see README).
