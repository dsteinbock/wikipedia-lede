# Lead-only identity classification

Determine the named subject's primary identity or reason for notability using
only the supplied lead sentence, rest of first lead paragraph, remaining lead
section, and infobox. Do not read the article body, classify death, resolve
QIDs, use tools or network access, or inspect other files.

The assignment manifest may contain one or more independent input files.
Classify every item and write one result object per item, in manifest order, as
a single JSON array. Do not mix facts between items. A one-item assignment
still uses a one-element array. Each result object is:

```json
{
  "schema_version": 1,
  "wikidata_id": "QID",
  "occupations": [{"label": "example identity", "qid": ""}],
  "reason": "concise explanation"
}
```

Prefer supplied canonical labels when accurate. Reason for notability outranks
incidental employment; keep multiple labels only when the highest-priority text
makes them equally central. Diseases, disabilities, physical traits, ethnicity,
and nationality are not identities. Leave every `qid` blank for deterministic
vocabulary resolution. Use exactly `[{"label":"somevalue","qid":""}]` only
when these inputs have no usable identity.
