# Team-Xero Blast Radius selector (static line)

Change-aware test selection for sqlglot `eb11aec` (RWX Blast Radius track).

## Provenance

This line starts from the MIT-licensed selector by jklein07
(https://github.com/jklein07/blast-radius, commit `b386a59`). Their copyright notice and licence
are kept in `LICENSE-jklein07`, and their description of the method is in `README-upstream.md`.
Changes made by Team-Xero on top of that base are in the commit history.

Team-Xero own selector (coverage maps plus in-container verification) is on the `master` branch.
We evaluated both on 1,076 labelled synthetic patches (zero missed failing tests for either) and
on malformed-patch edge cases before choosing this line.
