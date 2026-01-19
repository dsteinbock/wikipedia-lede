#!/usr/bin/env python3
"""
Wikipedia First Sentence Revision Analyzer with Caching
Analyzes the complete revision history of a Wikipedia article to track changes to the first sentence.
Uses intelligent caching to avoid refetching already-analyzed revisions.
"""

import requests
import re
from datetime import datetime
from collections import defaultdict
import json
import time
import os

def clean_sentence(sentence):
    """
    Clean up sentence to start with 'The 27 Club is'.
    Removes image references and other prefixes.
    """
    if not sentence:
        return None

    # Look for "The 27 Club is" or similar patterns
    patterns = [
        r'The 27 Club is',
        r'The 27 Club,',
        r'The "27 Club"',
        r"The '27 Club'",
    ]

    for pattern in patterns:
        match = re.search(pattern, sentence, re.IGNORECASE)
        if match:
            # Return everything from this point forward
            return sentence[match.start():].strip()

    # If no match found, check if it starts with "27 Club" variants
    if re.match(r'^27\s+[Cc]lub', sentence):
        return sentence.strip()

    # If it contains "The 27 Club" somewhere, extract from there
    if 'The 27 Club' in sentence or 'the 27 Club' in sentence:
        idx = sentence.lower().find('the 27 club')
        if idx >= 0:
            return sentence[idx:].strip()

    # Otherwise return as-is
    return sentence.strip()

def load_cache(cache_file):
    """Load previously analyzed revisions from cache file."""
    if not os.path.exists(cache_file):
        return {}

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Extract the revision cache
        # Old format: just has 'sentences' array
        # New format: has 'revision_cache' dict
        if 'revision_cache' in data:
            cache = data['revision_cache']
        else:
            # Migrate from old format - extract revision IDs from periods
            cache = {}
            if 'sentences' in data:
                for sent_data in data['sentences']:
                    for period in sent_data.get('periods', []):
                        if 'revid' in period:
                            revid = str(period['revid'])
                            # Clean the sentence
                            cleaned = clean_sentence(sent_data['sentence'])
                            cache[revid] = {
                                'timestamp': period['start'] + 'T00:00:00Z',
                                'sentence': cleaned
                            }

        print(f"Loaded cache with {len(cache)} previously analyzed revisions")

        # Clean up all cached sentences
        cleaned_cache = {}
        for revid, data in cache.items():
            cleaned_sentence = clean_sentence(data['sentence'])
            cleaned_cache[revid] = {
                'timestamp': data['timestamp'],
                'sentence': cleaned_sentence
            }

        if len(cleaned_cache) != len(cache):
            print(f"Cleaned {len(cache) - len(cleaned_cache)} invalid cache entries")

        return cleaned_cache

    except Exception as e:
        print(f"Error loading cache: {e}")
        return {}

def get_all_revision_ids(article_title):
    """Fetch all revision IDs and timestamps (lightweight query)."""
    base_url = "https://en.wikipedia.org/w/api.php"
    revisions = []

    headers = {
        'User-Agent': 'WikipediaFirstSentenceAnalyzer/2.0 (Educational research tool with caching)'
    }

    params = {
        'action': 'query',
        'titles': article_title,
        'prop': 'revisions',
        'rvprop': 'timestamp|ids',
        'rvlimit': 'max',
        'format': 'json',
        'rvdir': 'newer'
    }

    print(f"Fetching complete revision history for '{article_title}'...")

    while True:
        response = requests.get(base_url, params=params, headers=headers)

        if response.status_code != 200:
            print(f"Error: HTTP {response.status_code}")
            return []

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")
            return []

        pages = data['query']['pages']
        page_id = list(pages.keys())[0]

        if page_id == '-1':
            print(f"Error: Article '{article_title}' not found")
            return []

        page_revisions = pages[page_id].get('revisions', [])
        revisions.extend(page_revisions)

        if len(revisions) % 500 == 0:
            print(f"Fetched {len(revisions)} revision IDs...")

        if 'continue' in data:
            params['rvcontinue'] = data['continue']['rvcontinue']
        else:
            break

    print(f"Total revisions in article: {len(revisions)}")
    return revisions

def get_first_sentence_from_revision(revid, retry_count=0):
    """Get the first sentence from a specific revision using the parse API."""
    base_url = "https://en.wikipedia.org/w/api.php"

    headers = {
        'User-Agent': 'WikipediaFirstSentenceAnalyzer/2.0 (Educational research tool with caching)'
    }

    params = {
        'action': 'parse',
        'oldid': revid,
        'prop': 'text',
        'section': 0,
        'format': 'json',
        'disabletoc': 1
    }

    response = requests.get(base_url, params=params, headers=headers)

    # Handle rate limiting with exponential backoff
    if response.status_code == 429:
        if retry_count < 3:
            wait_time = (2 ** retry_count) * 5  # 5s, 10s, 20s
            print(f"  Rate limited, waiting {wait_time}s before retry...")
            time.sleep(wait_time)
            return get_first_sentence_from_revision(revid, retry_count + 1)
        else:
            print(f"  Failed after {retry_count} retries for revision {revid}")
            return None

    if response.status_code != 200:
        return None

    try:
        data = response.json()
    except json.JSONDecodeError:
        return None

    if 'parse' not in data or 'text' not in data['parse']:
        return None

    html_content = data['parse']['text']['*']

    # Extract plain text from HTML
    text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)

    # Decode HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#91;', '[')
    text = text.replace('&#93;', ']')

    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Extract first sentence
    match = re.search(r'^(.*?[.!?])(?:\s|$)', text)

    if match:
        sentence = match.group(1).strip()
    else:
        sentence = text[:500].strip() if text else None

    # Clean the sentence to start with "The 27 Club"
    return clean_sentence(sentence)

def analyze_with_cache(article_title, cache_file):
    """
    Analyze article with caching support.
    Only fetches revisions that haven't been analyzed before.
    """
    # Load existing cache
    cache = load_cache(cache_file)

    # Get all revision IDs from Wikipedia
    all_revisions = get_all_revision_ids(article_title)

    if not all_revisions:
        return

    # Determine which revisions need to be fetched
    cached_revids = set(cache.keys())
    all_revids = {str(rev['revid']) for rev in all_revisions}

    new_revids = all_revids - cached_revids

    print(f"\nCache statistics:")
    print(f"  Total revisions: {len(all_revisions)}")
    print(f"  Already cached: {len(cached_revids)}")
    print(f"  New to analyze: {len(new_revids)}")

    # Create revision lookup
    revid_to_timestamp = {str(rev['revid']): rev['timestamp'] for rev in all_revisions}

    # Fetch new revisions
    if new_revids:
        print(f"\nFetching {len(new_revids)} new revisions...")
        print(f"Estimated time: ~{len(new_revids) * 0.5 / 60:.1f} minutes")
        new_revids_list = sorted([int(r) for r in new_revids])

        for i, revid in enumerate(new_revids_list):
            if (i + 1) % 50 == 0:
                print(f"  Analyzed {i + 1}/{len(new_revids_list)} new revisions...")

                # Save cache checkpoint every 50 revisions
                if (i + 1) % 100 == 0:
                    checkpoint_data = {
                        'article': article_title,
                        'analysis_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'revision_cache': cache,
                        'note': 'Checkpoint save - analysis in progress'
                    }
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(checkpoint_data, f, indent=2, ensure_ascii=False)
                    print(f"  Checkpoint saved ({len(cache)} revisions cached)")

            timestamp = revid_to_timestamp.get(str(revid))
            if not timestamp:
                continue

            sentence = get_first_sentence_from_revision(revid)

            cache[str(revid)] = {
                'timestamp': timestamp,
                'sentence': sentence
            }

            # Rate limiting - be more conservative
            time.sleep(0.5)

        print(f"  Completed analysis of {len(new_revids_list)} new revisions")
    else:
        print("\nNo new revisions to fetch - using cached data only")

    # Build chronological timeline from cache
    print("\nBuilding timeline from complete revision history...")
    timeline = []

    for revid_str in sorted(cache.keys(), key=lambda x: revid_to_timestamp.get(x, '')):
        data = cache[revid_str]
        if data['sentence']:  # Only include revisions with valid sentences
            try:
                timestamp = datetime.strptime(data['timestamp'], '%Y-%m-%dT%H:%M:%SZ')
                timeline.append({
                    'revid': revid_str,
                    'timestamp': timestamp,
                    'sentence': data['sentence']
                })
            except:
                pass

    print(f"Timeline contains {len(timeline)} revisions with valid sentences")

    # Calculate durations
    sentence_durations = []
    last_sentence = None
    last_timestamp = None

    for i, item in enumerate(timeline):
        current_sentence = item['sentence']
        current_timestamp = item['timestamp']

        # Record when sentence changes
        if current_sentence != last_sentence:
            if last_sentence is not None:
                # Calculate duration of previous sentence
                duration_days = (current_timestamp - last_timestamp).days
                sentence_durations.append({
                    'sentence': last_sentence,
                    'start_date': last_timestamp,
                    'end_date': current_timestamp,
                    'days': duration_days
                })

            last_sentence = current_sentence
            last_timestamp = current_timestamp

    # Add final sentence period (to now)
    if last_sentence is not None:
        duration_days = (datetime.now() - last_timestamp).days
        sentence_durations.append({
            'sentence': last_sentence,
            'start_date': last_timestamp,
            'end_date': datetime.now(),
            'days': duration_days
        })

    print(f"Detected {len(sentence_durations)} distinct sentence change periods")

    # Group by unique sentences
    unique_sentences = defaultdict(lambda: {'days': 0, 'occurrences': [], 'total_occurrences': 0})

    for period in sentence_durations:
        sentence = period['sentence']
        if sentence:
            unique_sentences[sentence]['days'] += period['days']
            unique_sentences[sentence]['occurrences'].append({
                'start': period['start_date'].strftime('%Y-%m-%d'),
                'end': period['end_date'].strftime('%Y-%m-%d'),
                'days': period['days']
            })
            unique_sentences[sentence]['total_occurrences'] += 1

    # Sort by total days
    sorted_sentences = sorted(unique_sentences.items(), key=lambda x: x[1]['days'], reverse=True)

    # Print results
    print("\n" + "="*80)
    print(f"FIRST SENTENCE ANALYSIS: {article_title}")
    print("="*80)
    print(f"Total revisions: {len(all_revisions)}")
    print(f"Revisions with sentences: {len(timeline)}")
    print(f"Unique first sentences: {len(sorted_sentences)}")
    print("="*80)

    # Show top 15
    for i, (sentence, data) in enumerate(sorted_sentences[:15], 1):
        print(f"\n#{i} - Active for {data['days']} days | Appeared {data['total_occurrences']} time(s)")
        # Truncate long sentences for display
        display_sentence = sentence if len(sentence) <= 150 else sentence[:147] + "..."
        print(f"Sentence: {display_sentence}")
        print(f"Periods: {len(data['occurrences'])} total")
        # Show first few periods
        for occ in data['occurrences'][:3]:
            print(f"  • {occ['start']} to {occ['end']} ({occ['days']} days)")
        if len(data['occurrences']) > 3:
            print(f"  ... and {len(data['occurrences']) - 3} more periods")

    # Save to JSON with cache
    output = {
        'article': article_title,
        'analysis_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_revisions': len(all_revisions),
        'cached_revisions': len(cache),
        'revisions_with_sentences': len(timeline),
        'unique_sentences': len(sorted_sentences),
        'revision_cache': cache,  # Store the cache for next run
        'sentences': [
            {
                'sentence': sentence,
                'total_days': data['days'],
                'total_occurrences': data['total_occurrences'],
                'periods': data['occurrences']
            }
            for sentence, data in sorted_sentences
        ]
    }

    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*80}")
    print(f"Results saved to: {cache_file}")
    print(f"Cache updated with {len(cache)} analyzed revisions")
    print("="*80)

if __name__ == "__main__":
    article_title = "27 Club"
    cache_file = f"{article_title.replace(' ', '_')}_first_sentence_analysis.json"

    analyze_with_cache(article_title, cache_file)
