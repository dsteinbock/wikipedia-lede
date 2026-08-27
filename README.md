# Wikipedia research projects

This repository contains three projects that use Wikimedia data:

- [`lede-history/`](lede-history/) analyzes how the first sentence of the English Wikipedia article **27 Club** changed over time.
- [`age-27-musicians/`](age-27-musicians/) uses structured Wikidata statements to compile English Wikipedia subjects who definitely or narrowly possibly died at age 27 and had a music-related occupation.
- [`age-27-people/`](age-27-people/) uses the same structured-date rule for every human with an English Wikipedia article, regardless of occupation.

The two age-27 datasets share calendar and Wikimedia request helpers in [`wikidata_age27/`](wikidata_age27/). The historical analyzer remains independent because it uses the English Wikipedia Action API.
