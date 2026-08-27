# Age-27 musicians dataset

This project produces a structured-data-only list of people who have an English Wikipedia article, have a broadly music-related Wikidata occupation, and definitely or narrowly possibly died at age 27.

The checked-in dataset was generated on **2026-08-25** from the public Wikidata Query Service. It contains **156 people: 101 confirmed and 55 possible**. Wikidata data is available under [CC0](https://www.wikidata.org/wiki/Wikidata:Licensing); source: Wikidata.

## Generate the dataset

From the repository root:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r age-27-musicians/requirements.txt
python age-27-musicians/generate_csv.py
```

Successful responses are cached in `age-27-musicians/.cache/`. Remove that directory before running if you intentionally want a fresh Wikidata snapshot.

The output is [`age_27_musicians.csv`](age_27_musicians.csv).
Rows are sorted by earliest possible death date, ascending, with name and Wikidata ID used to break ties.

The CSV distinguishes Wikidata's specific [`cause of death` (`P509`)](https://www.wikidata.org/wiki/Property:P509) from its broad [`manner of death` (`P1196`)](https://www.wikidata.org/wiki/Property:P1196). Multiple best-ranked values are alphabetized and separated by semicolons. A blank value means Wikidata has no usable best-ranked item statement for that field; it is not an inference that the cause or manner is unknown in other sources.

## Rules

The program uses best-ranked Wikidata statements only. It does not read Wikipedia article prose.

An occupation qualifies when it is the same as, or a subclass of, musician (`Q639669`), music artist (`Q1294626`), lyricist (`Q822146`), or music director (`Q1198887`). Music educators, scoring assistants, radio DJs, and their subclasses are excluded. Exact generic disc jockey (`Q130857`) is also excluded, while performance-specific DJ subclasses remain eligible. The CSV retains only the most-specific qualifying occupations actually asserted for each person.

Structured day, month, and year precision becomes a possible date interval. A row is:

- `confirmed` when every allowed birth/death combination gives completed age 27;
- `possible` when age 27 is possible and the entire completed-age range remains within 26 through 28.

The readable range uses calendar years plus days since the last birthday. Day totals are also retained as exact minimum and maximum possible lifespan lengths.

## Limitations

“Complete” means complete relative to the Wikidata statements and English Wikipedia sitelinks returned by WDQS at generation time. Missing or incorrect Wikidata occupations, dates, sitelinks, or subclass relationships can cause omissions or misclassification. Query Service updates may lag Wikidata edits. Dates coarser than a year, unsupported calendar models, invalid chronology, and age ranges wider than the stated rule are excluded rather than guessed.

Data errors can be corrected on the corresponding Wikidata item identified by the CSV’s `wikidata_id` column.

## Tests

```bash
python -m unittest discover -s age-27-musicians/tests -v
```
