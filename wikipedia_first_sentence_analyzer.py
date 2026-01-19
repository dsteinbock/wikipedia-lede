#!/usr/bin/env python3
"""
Wikipedia First Sentence Revision Analyzer
Analyzes the revision history of a Wikipedia article to track changes to the first sentence.
This version uses the parse API for efficient, accurate text extraction.
"""

import requests
import re
from datetime import datetime
from collections import defaultdict
import json
import time

def get_revision_ids(article_title):
    """Fetch all revision IDs and timestamps (lightweight query)."""
    base_url = "https://en.wikipedia.org/w/api.php"
    revisions = []

    headers = {
        'User-Agent': 'WikipediaFirstSentenceAnalyzer/1.0 (Educational research tool)'
    }

    params = {
        'action': 'query',
        'titles': article_title,
        'prop': 'revisions',
        'rvprop': 'timestamp|ids',  # Only get IDs and timestamps, not content
        'rvlimit': 'max',
        'format': 'json',
        'rvdir': 'newer'
    }

    print(f"Fetching revision history for '{article_title}'...")

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

    print(f"Total revisions: {len(revisions)}")
    return revisions

def get_first_sentence_from_revision(revid):
    """Get the first sentence from a specific revision using the parse API."""
    base_url = "https://en.wikipedia.org/w/api.php"

    headers = {
        'User-Agent': 'WikipediaFirstSentenceAnalyzer/1.0 (Educational research tool)'
    }

    params = {
        'action': 'parse',
        'oldid': revid,
        'prop': 'text',
        'section': 0,  # Get only the lead section
        'format': 'json',
        'disabletoc': 1
    }

    response = requests.get(base_url, params=params, headers=headers)

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
    # Remove HTML tags but keep the text
    text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)

    # Decode HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')

    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Extract first sentence
    # Look for sentence-ending punctuation followed by space or end of string
    match = re.search(r'^(.*?[.!?])(?:\s|$)', text)

    if match:
        return match.group(1).strip()

    # If no sentence ending found, return first 200 chars
    return text[:200].strip() if text else None

def sample_revisions(revisions, sample_rate=10):
    """
    Sample revisions intelligently.
    - Always include first and last revision
    - Sample every Nth revision in between
    - Include revisions around major edit counts
    """
    if len(revisions) <= sample_rate * 2:
        return revisions

    sampled = [revisions[0]]  # Always include first

    # Sample middle revisions
    for i in range(sample_rate, len(revisions) - 1, sample_rate):
        sampled.append(revisions[i])

    # Always include last
    if revisions[-1] not in sampled:
        sampled.append(revisions[-1])

    return sampled

def analyze_first_sentence_history(article_title, sample_rate=25):
    """
    Analyze the history of the first sentence.
    Uses sampling to reduce API load while still capturing major changes.
    """
    revisions = get_revision_ids(article_title)

    if not revisions:
        return

    # Sample revisions to reduce API calls
    sampled_revisions = sample_revisions(revisions, sample_rate)
    print(f"\nAnalyzing {len(sampled_revisions)} sampled revisions (every ~{sample_rate} revisions)...")

    sentence_changes = []
    last_sentence = None

    for i, rev in enumerate(sampled_revisions):
        revid = rev['revid']
        timestamp = datetime.strptime(rev['timestamp'], '%Y-%m-%dT%H:%M:%SZ')

        if (i + 1) % 10 == 0:
            print(f"Analyzed {i + 1}/{len(sampled_revisions)} revisions...")

        first_sentence = get_first_sentence_from_revision(revid)

        # Only record when the sentence changes
        if first_sentence != last_sentence:
            sentence_changes.append({
                'timestamp': timestamp,
                'sentence': first_sentence,
                'revid': revid
            })
            last_sentence = first_sentence

        # Be nice to Wikipedia's servers
        time.sleep(0.1)

    if not sentence_changes:
        print("No sentences found")
        return

    # Calculate durations
    sentence_durations = []

    for i in range(len(sentence_changes)):
        current = sentence_changes[i]

        if i < len(sentence_changes) - 1:
            next_change = sentence_changes[i + 1]['timestamp']
        else:
            next_change = datetime.now()

        duration_days = (next_change - current['timestamp']).days

        sentence_durations.append({
            'sentence': current['sentence'],
            'start_date': current['timestamp'],
            'end_date': next_change,
            'days': duration_days,
            'revid': current['revid']
        })

    # Group by unique sentences
    unique_sentences = defaultdict(lambda: {'days': 0, 'occurrences': [], 'total_occurrences': 0})

    for period in sentence_durations:
        sentence = period['sentence']
        if sentence:
            unique_sentences[sentence]['days'] += period['days']
            unique_sentences[sentence]['occurrences'].append({
                'start': period['start_date'].strftime('%Y-%m-%d'),
                'end': period['end_date'].strftime('%Y-%m-%d'),
                'days': period['days'],
                'revid': period['revid']
            })
            unique_sentences[sentence]['total_occurrences'] += 1

    # Sort by total days
    sorted_sentences = sorted(unique_sentences.items(), key=lambda x: x[1]['days'], reverse=True)

    # Print results
    print("\n" + "="*80)
    print(f"FIRST SENTENCE ANALYSIS: {article_title}")
    print("="*80)
    print(f"Total revisions in article: {len(revisions)}")
    print(f"Revisions analyzed: {len(sampled_revisions)}")
    print(f"Sentence changes detected: {len(sentence_changes)}")
    print(f"Unique first sentences: {len(sorted_sentences)}")
    print("="*80)

    # Show top 10
    for i, (sentence, data) in enumerate(sorted_sentences[:10], 1):
        print(f"\n#{i} - Active for {data['days']} days | Appeared {data['total_occurrences']} time(s)")
        print(f"Sentence: {sentence}")
        print(f"Periods:")
        for occ in data['occurrences']:
            print(f"  • {occ['start']} to {occ['end']} ({occ['days']} days) [rev {occ['revid']}]")

    # Save to JSON
    output = {
        'article': article_title,
        'analysis_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_revisions': len(revisions),
        'analyzed_revisions': len(sampled_revisions),
        'sample_rate': sample_rate,
        'unique_sentences': len(sorted_sentences),
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

    output_file = f"{article_title.replace(' ', '_')}_first_sentence_analysis.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*80}")
    print(f"Results saved to: {output_file}")
    print("="*80)

if __name__ == "__main__":
    article_title = "27 Club"
    # Sample every 25 revisions to be respectful of Wikipedia's API
    # Adjust sample_rate higher (e.g., 50) for faster analysis
    # or lower (e.g., 10) for more detailed history
    analyze_first_sentence_history(article_title, sample_rate=25)
