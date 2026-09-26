# chromonic patches to Taffy 0.14.0

Each change is marked `chromonic patch` in the source.

- `src/compute/block.rs`, absolute layout: two auto vertical margins split the
  remaining space equally even when it is negative (CSS 2.1 10.6.4). Upstream
  zeroes them, which is only the horizontal rule (CSS 2.1 10.3.7).
