# Death evidence extraction

Read the complete supplied selected-article packet. Extract every passage that
bears on the named subject's cause, manner, or circumstances of death. Include
competing accounts, uncertainty, suspected or probable accounts, pending
investigations, and explicit statements that the cause is unknown or unreported.
Exclude other people's deaths or injuries, navigation text, filenames, and
unrelated medical history.

Do not classify, diagnose, map vocabulary, use keyword rules, use tools or
network access, or inspect files outside the assignment. The assignment
manifest may contain one or more independent input files. Process every item
and write one result object per item, in manifest order, as a single JSON
array. Do not mix evidence between items. A one-item assignment still uses a
one-element array. Each result object is:

```json
{
  "schema_version": 1,
  "wikidata_id": "QID",
  "evidence": [
    {
      "evidence_id": "E1",
      "source_tier": "lead_sentence",
      "section": "section name",
      "text": "complete relevant passage"
    }
  ],
  "no_usable_account": {"cause": false, "manner": false},
  "reason": "concise completeness note"
}
```

Allowed tiers are `lead_sentence`, `rest_of_lead_paragraph`,
`remaining_lead_section`, `infobox`, and `rest_of_article`. Use sequential IDs
in source order. Higher tiers take precedence, but retain lower-tier competing
or qualifying evidence. A speculative, suspected, probable, reported, inferred,
or competing account is usable evidence; this is not a beyond-a-reasonable-doubt
review. Set a `no_usable_account` field to true only when the complete article
contains literally no possible account for it, or explicitly says it is unknown
or undisclosed without offering any theory. A cause or death circumstance that
supports a direct manner inference makes the manner usable even if the article
does not separately label the manner. The evidence array is empty only when
neither field has relevant text. Retain explicit unknown or unreported passages,
but do not set a field's flag true when any possible account also appears.
