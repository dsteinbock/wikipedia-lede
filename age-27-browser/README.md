# 27ish Club

A dependency-free local website for exploring the checked-in age-27 people and
musician datasets. Cause, manner, and primary identity use structured Wikidata
values first and the approved Wikipedia fields as field-by-field fallbacks.
Literal `somevalue` fallbacks render as `unknown`.

The same effective values drive search, occupation filters, charts, aggregates,
member cards, and details. Wikipedia-derived detail values carry a compact `¹`
marker and footnote. Every member row and detail dialog also links directly to
the person's Wikipedia article; the row link is a separate control and does not
open the detail dialog.

Open `index.html` directly, or serve the repository root locally:

```bash
python3 -m http.server 8000
```

Then visit <http://localhost:8000/age-27-browser/>.

After either CSV changes, rebuild the embedded browser payload:

```bash
python3 age-27-browser/build_data.py
```

Run the payload tests from the repository root:

```bash
python -m unittest discover -s age-27-browser/tests -v
```
