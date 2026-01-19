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

Run the script:
```bash
python wikipedia_first_sentence_analyzer.py
```

By default, it analyzes the "27 Club" article. To analyze a different article, edit the last line of the script:
```python
article_title = "Your Article Title"
```

### Configuration

You can adjust the sampling rate in the script:
- Lower sample_rate (e.g., 10) = more detailed analysis, more API calls
- Higher sample_rate (e.g., 50) = faster analysis, fewer API calls

Default is 25, which samples every 25th revision.

## Output

The script generates two outputs:

1. **Terminal output**: Top 10 most persistent first sentences with their active periods
2. **JSON file**: Complete data for all unique sentences (`{Article_Name}_first_sentence_analysis.json`)

## API Respectfulness

This script is designed to be respectful of Wikipedia's resources:
- Uses lightweight queries to fetch revision metadata first
- Only fetches content for sampled revisions
- Includes 0.1s delay between API calls
- Uses proper User-Agent header
- Samples ~4% of revisions by default

## Example Output

```
Total revisions in article: 5127
Revisions analyzed: 207
Sentence changes detected: 108
Unique first sentences: 61

#1 - Active for 547 days | Appeared 6 time(s)
Sentence: The 27 Club is a group of popular musicians who died at age 27...
```
