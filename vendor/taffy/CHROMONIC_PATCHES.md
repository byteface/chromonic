# chromonic patches to Taffy 0.14.0

Each change is marked `chromonic patch` in the source.

- `src/compute/block.rs`, absolute layout: two auto vertical margins split the
  remaining space equally even when it is negative (CSS 2.1 10.6.4). Upstream
  zeroes them, which is only the horizontal rule (CSS 2.1 10.3.7).
- `src/compute/float.rs` / `src/compute/block.rs`, a box establishing an
  independent formatting context beside floats: its slot avoids every float
  alongside its whole border-box height, not only the float segment at its
  top (CSS 2.1 9.5). `find_bfc_slot` takes the box's height; block layout
  measures the box at the slot's width and re-checks until the slot is stable.
- `src/compute/float.rs`, `clear`: the segment search no longer starts past the
  cleared side's last float segment + 1 (which skipped a segment a float or box
  still fit beside, e.g. segment 0 when no float is on that side); the y-based
  `cleared_threshold` already enforces clearance.
