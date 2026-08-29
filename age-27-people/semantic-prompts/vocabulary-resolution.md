# Novel vocabulary resolution

Resolve only the supplied novel `(field, label)` against the supplied
Wikidata Search candidates. Do not search, inspect articles, change the concept,
classify a person, use tools, or inspect other files.

The assignment manifest may contain one or more independent input files.
Resolve every item and write one result object per item, in manifest order, as
a single JSON array. Do not mix candidates between items. A one-item assignment
still uses a one-element array. Each result object is:

```json
{
  "schema_version": 1,
  "field": "cause",
  "label": "example label",
  "decision": "approved",
  "selected_qid": "Q123",
  "reason": "concise semantic match explanation"
}
```

`field` is one of `cause`, `manner`, or `occupation`. Preserve the supplied
`label` exactly. `decision` is `approved` or `no_adequate_candidate`; for the
latter, `selected_qid` is blank. Approve only a supplied candidate whose
canonical label and description express the proposed concept in the requested
field. Never broaden, narrow, or replace the concept merely to make a candidate
fit.
