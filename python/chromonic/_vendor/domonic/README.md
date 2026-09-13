# Temporary Domonic snapshot

These files are copied from the pending Domonic 1.8.1 worktree at commit base
`1.8.0`. They carry the DOM/style invalidation epochs, computed-style caches,
and layout-box used-value fixes Chromonic needs before those changes have an
upstream release.

`dom.py` and `style.py` are the requested overrides. `_cssom.py` and
`layout.py` are included because the new code calls their epoch and layout-box
APIs. `chromonic._domonic_vendor` substitutes only these modules during import;
the installed `domonic-libs` package supplies everything else.

Remove the bootstrap and this directory once the equivalent Domonic release is
the minimum supported dependency.

