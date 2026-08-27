#!/usr/bin/env python3
"""
Generate CSV export of Wikipedia first sentence analysis.
Creates a chronological view of all sentences with their first appearance date and total days active.
"""

import json
import csv
import sys
from pathlib import Path


def generate_csv(json_file, output_csv=None):
    """
    Generate CSV from analysis JSON file.

    Args:
        json_file: Path to the analysis JSON file
        output_csv: Output CSV path (defaults to same name with .csv extension)
    """
    # Load the analysis data
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Prepare data for CSV
    rows = []
    for sent_data in data['sentences']:
        sentence = sent_data['sentence']
        total_days = sent_data['total_days']
        periods = sent_data['periods']

        # Get the first appearance date (earliest start date across all periods)
        if periods:
            first_appearance = min(period['start'] for period in periods)
            rows.append({
                'first_appearance': first_appearance,
                'total_days_active': total_days,
                'num_periods': sent_data['total_occurrences'],
                'sentence': sentence
            })

    # Sort by first appearance date (chronological order)
    rows.sort(key=lambda x: x['first_appearance'])

    # Determine output filename
    if output_csv is None:
        output_csv = Path(json_file).stem + '_chronological.csv'

    # Write to CSV
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['first_appearance', 'total_days_active', 'num_periods', 'sentence'])
        writer.writeheader()
        writer.writerows(rows)

    return output_csv, rows


def print_summary(rows):
    """Print summary statistics of the CSV data."""
    total = len(rows)
    active = [int(r['total_days_active']) for r in rows]
    periods = [int(r['num_periods']) for r in rows]

    print("=" * 80)
    print("CSV EXPORT SUMMARY")
    print("=" * 80)
    print(f"Total sentences: {total}")
    print(f"Date range: {rows[0]['first_appearance']} to {rows[-1]['first_appearance']}")
    print(f"\nDays active:")
    print(f"  Average: {sum(active)/len(active):.1f} days")
    print(f"  Median: {sorted(active)[len(active)//2]} days")
    print(f"  Max: {max(active)} days")
    print(f"  Min: {min(active)} days")
    print(f"\nDistribution:")
    print(f"  0 days (immediately reverted): {sum(1 for d in active if d == 0)} ({sum(1 for d in active if d == 0)/total*100:.0f}%)")
    print(f"  1-99 days: {sum(1 for d in active if 1 <= d < 100)} ({sum(1 for d in active if 1 <= d < 100)/total*100:.0f}%)")
    print(f"  100+ days (persistent): {sum(1 for d in active if d >= 100)} ({sum(1 for d in active if d >= 100)/total*100:.0f}%)")
    print("=" * 80)


if __name__ == "__main__":
    # Default to the 27 Club analysis file
    json_file = "27_Club_first_sentence_analysis.json"

    # Allow custom input file via command line
    if len(sys.argv) > 1:
        json_file = sys.argv[1]

    # Check if file exists
    if not Path(json_file).exists():
        print(f"Error: {json_file} not found")
        print("Run the analyzer first: python wikipedia_first_sentence_analyzer.py --full")
        sys.exit(1)

    # Generate CSV
    print(f"Generating CSV from {json_file}...")
    output_csv, rows = generate_csv(json_file)

    print(f"✓ Created: {output_csv}")
    print()

    # Print summary
    print_summary(rows)
