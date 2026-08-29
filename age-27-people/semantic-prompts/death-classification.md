# Death classification

Classify only the named subject's death from the supplied evidence bundle. Do
not read the article, search, verify excerpts, resolve QIDs, use tools, or infer
from programmed keywords. Prefer supplied canonical labels when accurate;
propose a new plain-English label only when none fits.

The assignment manifest may contain one or more independent input files.
Classify every item and write one result object per item, in manifest order, as
a single JSON array. Do not mix evidence between items. A one-item assignment
still uses a one-element array. Each result object is:

```json
{
  "schema_version": 1,
  "wikidata_id": "QID",
  "cause": [{"label": "example cause", "qid": ""}],
  "manner": [{"label": "example manner", "qid": ""}],
  "status": "settled",
  "evidence_ids": {
    "cause": ["E1"],
    "manner": ["E1"]
  },
  "reason": "concise rationale"
}
```

Allowed statuses are `settled` and `unknown`. Use
JSON `null` for a field and an empty evidence-ID list only when that field is
marked already effective. A required field is a nonempty array of `{label, qid}`
objects; genuine unknown is exactly `[{"label":"somevalue","qid":""}]`.
Preserve multiple causal layers when supported. Manner is the broad
circumstance: natural causes, accident, suicide, homicide, execution, or
another supported canonical concept. Infer manner directly from a reported
cause or the described circumstances; manner does not require a separate
explicit statement or separate evidence passage.

If `cause` contains any concrete label, always output a concrete `manner` that
logically follows from that cause. Treat the reported cause as accurate for this
purpose, even when the article describes it as possible, preliminary, or under
investigation. Do not output `somevalue` for manner merely because the article
lacks an explicit manner statement; reserve it for cases where the cause itself
is `somevalue` or no logical manner can follow from the supplied cause.

- `settled`: use whenever the evidence contains any possible cause or manner,
  including an account described as reported, speculative, suspected, probable,
  pending, or necessarily inferred. This is not a legal proof standard.
- `unknown`: use only when neither cause nor manner has any usable account, or
  the article explicitly says the death is unknown or undisclosed without
  offering any theory. One concrete field is enough to make the overall status
  `settled`; when cause is concrete, manner must also be inferred as described
  above.

Never output `provisional` or `disputed`. When accounts compete, choose the
project-preferred supported account and mark it `settled`. Prefer the more
inflammatory account reflected by friends, family, the press, activists, or
popular perception over a conflicting government, police, military, or other
official account. In particular, prefer homicide over suicide, accident, or
natural causes, and prefer overdose over suicide or illness.

Leave `qid` blank; deterministic vocabulary resolution fills it later. Never
use `somevalue` because a label lacks a QID. Reference only supplied evidence
IDs; use an empty evidence-ID list only for a genuine `somevalue`. A needed
field may be concrete despite its extraction flag when it can be inferred from
evidence for the other field. Use `somevalue` only when its corresponding
`no_usable_account` flag is true.
