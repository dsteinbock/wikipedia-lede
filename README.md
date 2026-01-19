# Wikipedia First Sentence Analyzer

Analyzes how the first sentence of a Wikipedia article has evolved over time by examining its revision history.

## Features

- Tracks all changes to the first sentence across the article's history
- Shows how long each version was active
- Uses efficient sampling to reduce API load
- Respects Wikipedia's API guidelines with rate limiting

## Setup

1. Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install requests
```

## Usage

The script has three modes:

### Test Mode (Default)
Analyzes every 100th revision for quick testing (~30 seconds):
```bash
python wikipedia_first_sentence_analyzer.py
# or explicitly:
python wikipedia_first_sentence_analyzer.py --test
```

### Full Mode
Analyzes all ~5,000 revisions (~40 minutes):
```bash
python wikipedia_first_sentence_analyzer.py --full
```

### Caching
The script caches all analyzed revisions. Subsequent runs only fetch new revisions since the last run, making updates very fast.

To analyze a different article, edit this line in the script:
```python
article_title = "Your Article Title"
```

## Output

The script generates two outputs:

1. **Terminal output**: Top 10 most persistent first sentences with their active periods
2. **JSON file**: Complete data for all unique sentences (`{Article_Name}_first_sentence_analysis.json`)

## Features in Detail

### Intelligent Sentence Extraction
- Removes image captions and metadata from parsed HTML
- Automatically finds the actual article text (starting with "The 27 Club")
- Cleans HTML entities and formatting
- Ensures consistent sentence beginnings

### Smart Caching
- Stores analyzed revisions in JSON
- Only fetches new revisions on subsequent runs
- Automatically migrates old cache formats
- Checkpoint saves every 100 revisions

## API Respectfulness

This script is designed to be respectful of Wikipedia's resources:
- Uses lightweight queries to fetch revision metadata first
- Caches results to avoid re-fetching
- Includes 0.5s delay between API calls with exponential backoff for rate limits
- Uses proper User-Agent header
- Test mode samples only 1% of revisions by default

## Example Output

```
Total revisions in article: 5127
Revisions analyzed: 207
Sentence changes detected: 108
Unique first sentences: 61

#1 - Active for 547 days | Appeared 6 time(s)
Sentence: The 27 Club is a group of popular musicians who died at age 27...
```
