#!/usr/bin/env python3
"""
Wikipedia First Sentence Revision Analyzer
Analyzes the revision history of a Wikipedia article to track changes to the first sentence.
"""

import requests
import re
from datetime import datetime
from collections import defaultdict
import json

def get_all_revisions(article_title):
    """Fetch all revisions of a Wikipedia article."""
    base_url = "https://en.wikipedia.org/w/api.php"
    revisions = []
    
    params = {
        'action': 'query',
        'titles': article_title,
        'prop': 'revisions',
        'rvprop': 'timestamp|content',
        'rvlimit': 'max',
        'format': 'json',
        'rvdir': 'newer'  # Start from oldest
    }
    
    print(f"Fetching revision history for '{article_title}'...")
    
    while True:
        response = requests.get(base_url, params=params)
        data = response.json()
        
        pages = data['query']['pages']
        page_id = list(pages.keys())[0]
        
        if page_id == '-1':
            print(f"Error: Article '{article_title}' not found")
            return []
        
        page_revisions = pages[page_id].get('revisions', [])
        revisions.extend(page_revisions)
        
        print(f"Fetched {len(revisions)} revisions so far...")
        
        # Check if there are more revisions
        if 'continue' in data:
            params['rvcontinue'] = data['continue']['rvcontinue']
        else:
            break
    
    print(f"Total revisions fetched: {len(revisions)}")
    return revisions

def extract_first_sentence(wikitext):
    """Extract the first sentence from Wikipedia wikitext."""
    if not wikitext:
        return None
    
    # Remove leading templates, infoboxes, and other non-prose content
    # Remove {{ }} blocks (templates/infoboxes)
    text = re.sub(r'\{\{[^}]*\}\}', '', wikitext, flags=re.DOTALL)
    
    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    
    # Remove file/image references
    text = re.sub(r'\[\[File:.*?\]\]', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[\[Image:.*?\]\]', '', text, flags=re.IGNORECASE | re.DOTALL)
    
    # Strip leading whitespace and newlines
    text = text.lstrip()
    
    # Find the first sentence (ending with . ! or ?)
    # Handle wiki links [[text]] and [[link|text]]
    sentence_match = re.search(r'^(.*?[.!?])\s', text, re.DOTALL)
    
    if not sentence_match:
        # Try to get first line if no sentence ending found
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('=') and not line.startswith('{') and not line.startswith('|'):
                return clean_wikitext(line[:500])  # Limit length
        return None
    
    first_sentence = sentence_match.group(1)
    return clean_wikitext(first_sentence)

def clean_wikitext(text):
    """Clean wikitext markup from a sentence."""
    # Remove reference tags
    text = re.sub(r'<ref[^>]*>.*?</ref>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<ref[^>]*/?>', '', text, flags=re.IGNORECASE)
    
    # Convert wiki links [[Link|Display]] to Display, [[Link]] to Link
    text = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', text)
    
    # Remove bold/italic markup
    text = re.sub(r"'{2,}", '', text)
    
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    
    return text

def analyze_first_sentence_history(article_title):
    """Analyze the history of the first sentence of a Wikipedia article."""
    revisions = get_all_revisions(article_title)
    
    if not revisions:
        return
    
    # Process revisions chronologically
    sentence_periods = []
    
    for i, revision in enumerate(revisions):
        timestamp = datetime.strptime(revision['timestamp'], '%Y-%m-%dT%H:%M:%SZ')
        content = revision.get('*', '')
        first_sentence = extract_first_sentence(content)
        
        sentence_periods.append({
            'timestamp': timestamp,
            'sentence': first_sentence,
            'revision_id': i + 1
        })
    
    # Calculate duration for each unique sentence
    sentence_durations = []
    
    for i in range(len(sentence_periods)):
        current = sentence_periods[i]
        
        # Find when this sentence changed
        if i < len(sentence_periods) - 1:
            next_change = sentence_periods[i + 1]['timestamp']
        else:
            # Last revision - use current date
            next_change = datetime.now()
        
        duration_days = (next_change - current['timestamp']).days
        
        sentence_durations.append({
            'sentence': current['sentence'],
            'start_date': current['timestamp'],
            'end_date': next_change,
            'days': duration_days
        })
    
    # Group by unique sentences and sum durations
    unique_sentences = defaultdict(lambda: {'days': 0, 'occurrences': [], 'total_occurrences': 0})
    
    for period in sentence_durations:
        sentence = period['sentence']
        if sentence:  # Skip None/empty sentences
            unique_sentences[sentence]['days'] += period['days']
            unique_sentences[sentence]['occurrences'].append({
                'start': period['start_date'].strftime('%Y-%m-%d'),
                'end': period['end_date'].strftime('%Y-%m-%d'),
                'days': period['days']
            })
            unique_sentences[sentence]['total_occurrences'] += 1
    
    # Sort by total days (descending)
    sorted_sentences = sorted(unique_sentences.items(), key=lambda x: x[1]['days'], reverse=True)
    
    # Print results
    print("\n" + "="*80)
    print(f"FIRST SENTENCE ANALYSIS: {article_title}")
    print("="*80)
    print(f"Total revisions analyzed: {len(revisions)}")
    print(f"Unique first sentences: {len(sorted_sentences)}")
    print("="*80)
    
    for i, (sentence, data) in enumerate(sorted_sentences, 1):
        print(f"\n#{i} - Total Days: {data['days']} | Occurrences: {data['total_occurrences']}")
        print(f"Sentence: {sentence}")
        print(f"Periods:")
        for occ in data['occurrences']:
            print(f"  • {occ['start']} to {occ['end']} ({occ['days']} days)")
    
    # Save to JSON file
    output = {
        'article': article_title,
        'analysis_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_revisions': len(revisions),
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
    analyze_first_sentence_history(article_title)
