# Ambiguous-member Wikipedia evidence review

You receive one or more narrow JSON inputs produced by the deterministic
ambiguous-member scanner. Review only the supplied excerpts. Do not use tools,
network access, shared files, outside knowledge, or unstated arithmetic.

For every input, return exactly one object with these keys:

```json
{
  "schema_version": 1,
  "wikidata_id": "Q123",
  "candidate_reviews": [
    {
      "candidate_id": "C001",
      "verdict": "confirmed",
      "claim_type": "age_at_death",
      "reason": "The sentence explicitly says the subject was 27 when he died."
    }
  ],
  "reason": "Concise overall explanation."
}
```

Return one `candidate_reviews` entry for every supplied candidate, in the same
order and with the exact candidate ID. Use only these values:

- `verdict`: `confirmed`, `rejected`, or `ambiguous`
- `claim_type`: `age_at_death`, `birth_date`, `death_date`, or `other`

For an age candidate, `confirmed` means the excerpt explicitly states the
biographical subject's age at death. Wording can be as short as “he was 27”
when death context makes the reference clear. Reject jersey numbers, dates,
counts, ranks, another person's age, and ages at events other than death. Use
`ambiguous` when the excerpt plausibly refers to the subject's age at death but
does not clearly establish it. Classify each singular statement independently;
the deterministic finalizer, not this review, decides whether confirmed
singular ages conflict. Age ranges are non-singular and are not semantic age
candidates.

A citation title is not weaker merely because it is a title. Treat concise
headlines such as “Actor Name dead at 27”, “passed away at 27”, or an identified
subject described as a 28-year-old who died as explicit age-at-death evidence.
Use `ambiguous` only when the title does not identify the subject or does not
actually connect the number to death.

For a date candidate, `confirmed` means the excerpt explicitly gives the
subject's birth date or death date; choose that exact claim type. Reject dates
for publications, battles, relatives, citations, or other events. Use
`ambiguous` when the date could be the subject's birth/death date but the
excerpt does not clearly establish which. Never improve precision, normalize a
different value, calculate an age, or introduce a date not already supplied.

Use `claim_type: other` for every rejected candidate and normally for an
ambiguous candidate whose intended claim type cannot be established. Keep each
reason specific to the supplied excerpt. Return one JSON array in assignment
manifest order and no prose outside the array.
