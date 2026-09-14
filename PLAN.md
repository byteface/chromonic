# TODO - new plans for the project

# Find wrinkles in the domonic API and log them so they can be fixed upstream.

## domonic issues found while comparing eventual.technology

- `ComputedStyleDeclaration` / CSSOM media rule matching caches `_cssom_rule_index` on the document without automatically invalidating it when `window.innerWidth` / `innerHeight` changes. Browser embedders must currently call their viewport setter and manually clear `document._cssom_rule_index` before resolving styles after a resize, otherwise `@media` rules can be evaluated against a stale viewport.
- `CSSMediaRule` traversal in domonic appears to prefer `conditionText` (a plain string) over the parsed `media` object. Because the style walker only checks a `.matches` attribute and strings do not have one, media blocks can be treated as matching unconditionally. This shows up on Bootstrap breakpoints while comparing `eventual.technology`.
