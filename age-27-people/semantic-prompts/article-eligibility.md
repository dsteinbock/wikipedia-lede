# Article eligibility

Review only the supplied article packet for the named dataset subject. Describe
the page; do not select an article, propose removal, classify death or identity,
use tools or network access, or inspect other files.

The assignment manifest may contain one or more independent input files. Review
every item and write one result object per item, in manifest order, as a single
JSON array to the assigned output path. Do not mix facts between items. A
one-item assignment still uses a one-element array. Each result object is:

```json
{
  "schema_version": 1,
  "wikidata_id": "QID",
  "reviews": {
    "candidate_id": {
      "page_kind": "person",
      "subject_match": "match",
      "subject_is_human": "human",
      "life_status": "deceased",
      "age_compatibility": "compatible",
      "reason": "concise explanation"
    }
  }
}
```

Review exactly the supplied candidates. For the English assignment, `reviews`
contains only `enwiki`. For an alternate assignment, it contains exactly every
supplied alternate candidate and must not contain `enwiki`. Do not combine the
two assignments.

Allowed page kinds are `person`,
`event`, `case`, `list`, `group`, and `other`; subject match is `match`,
`mismatch`, or `unclear`; human status is `human`, `nonhuman`, or `unclear`;
life status is `deceased`, `living`, `conflicting`, or `unclear`; age status is
`compatible`, `outside_26_28`, `conflicting`, or `unknown`.

`person` means a dedicated biography of the intended individual; a redirect to
an event, case, list, group, or another person is not one. A translated name or
redirected biography title may match. Mark age outside only when its possible
interval is disjoint from 26–28. Leave selection and removal policy to the
controller.
