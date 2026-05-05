
import argparse
import sys
import pandas as pd

CATEGORIES = ["gross_motor", "fine_motor", "speech_language", "social_communication", "self_help"]

def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    # Basic validation
    required_cols = [
        "age_range_months","max_age_months","milestone_category","milestone","observed_issue",
        "recommended_therapy",
        "activity_1_name","activity_1_url","duration_1","frequency_1",
        "activity_2_name","activity_2_url","duration_2","frequency_2",
        "activity_3_name","activity_3_url","duration_3","frequency_3",
        "red_flags","source_urls"
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    # Clean/normalize ages
    df["max_age_months"] = pd.to_numeric(df["max_age_months"], errors="coerce")
    df = df.dropna(subset=["max_age_months"]).copy()
    df["max_age_months"] = df["max_age_months"].astype(int)

    # Strip whitespace in text fields
    for c in ["milestone_category","milestone","observed_issue","recommended_therapy",
              "activity_1_name","activity_1_url","duration_1","frequency_1",
              "activity_2_name","activity_2_url","duration_2","frequency_2",
              "activity_3_name","activity_3_url","duration_3","frequency_3",
              "source_urls"]:
        df[c] = df[c].astype(str).str.strip()

    return df

def pick_representative_rows(df: pd.DataFrame, category: str) -> pd.DataFrame:
    """For each available age within a category, pick the first row as a representative milestone+activities."""
    sub = df[df["milestone_category"] == category].copy()
    # Sort by age to ensure deterministic order
    sub = sub.sort_values(["max_age_months", "milestone"]).copy()
    # Take first row per age bucket
    rep = sub.drop_duplicates(subset=["max_age_months"], keep="first").copy()
    return rep[["max_age_months","milestone","recommended_therapy",
                "activity_1_name","activity_1_url","duration_1","frequency_1",
                "activity_2_name","activity_2_url","duration_2","frequency_2",
                "activity_3_name","activity_3_url","duration_3","frequency_3",
                "source_urls"]].reset_index(drop=True)

def ask_yes_no(prompt: str) -> bool:
    while True:
        ans = input(prompt + " (y/n): ").strip().lower()
        if ans in ["y", "yes"]:
            return True
        if ans in ["n", "no"]:
            return False
        print("Please answer 'y' or 'n'.")

def nearest_age_index(ages, target):
    """Return the index of the nearest age value to target."""
    # ages is sorted list of ints
    diffs = [abs(a - target) for a in ages]
    return diffs.index(min(diffs))

def find_functional_level_for_category(rep_df: pd.DataFrame, starting_age: int) -> int:
    """
    Binary-search style: If the child meets the milestone at an age, search higher;
    if not, search lower. We return the highest age at which the child meets the milestone.
    If never meets any, we return the minimum age.
    """
    ages = rep_df["max_age_months"].tolist()
    if not ages:
        raise ValueError("No ages available for this category.")
    ages = sorted(set(ages))
    lo = 0
    hi = len(ages) - 1
    # start near the reported age
    # nearest index to starting age
    diffs = [abs(a - starting_age) for a in ages]
    mid = diffs.index(min(diffs))

    best_met_idx = -1  # track highest index where milestone met

    # We'll do a capped number of iterations
    iterations = 0
    max_iter = max(10, len(ages) + 3)
    used_indices = set()

    while iterations < max_iter:
        iterations += 1
        idx = mid
        if idx in used_indices:
            # If we've already asked this index, move slightly
            idx = min(idx + 1, hi)
            if idx in used_indices:
                idx = max(mid - 1, lo)
                if idx in used_indices:
                    break
        used_indices.add(idx)

        age = ages[idx]
        row = rep_df[rep_df["max_age_months"] == age].iloc[0]
        q = f"""At around {age} months, typical milestone for this category is: "{row['milestone']}".
Would you say your child currently meets this?"""
        meets = ask_yes_no(q)

        if meets:
            best_met_idx = max(best_met_idx, idx)
            # search higher
            if idx == hi:
                break
            lo = max(lo, idx + 1)
        else:
            # search lower
            if idx == lo:
                break
            hi = min(hi, idx - 1)

        if lo > hi:
            break

        mid = (lo + hi) // 2

    if best_met_idx == -1:
        # never met any milestone asked; return the minimum age as baseline
        return ages[0]
    else:
        return ages[best_met_idx]

def print_category_summary(category: str, rep_df: pd.DataFrame, level_age: int):
    row = rep_df[rep_df["max_age_months"] == level_age].iloc[0]
    print("\n" + "="*72)
    print(f"[{category.replace('_',' ').title()}] Estimated functional level: ~{level_age} months")
    print("- Suggested therapies/activities:")
    acts = [
        (row["activity_1_name"], row["activity_1_url"], row["duration_1"], row["frequency_1"]),
        (row["activity_2_name"], row["activity_2_url"], row["duration_2"], row["frequency_2"]),
        (row["activity_3_name"], row["activity_3_url"], row["duration_3"], row["frequency_3"]),
    ]
    for i, (name, url, dur, freq) in enumerate(acts, start=1):
        if not name:
            continue
        dur = (dur or "").strip()
        freq = (freq or "").strip()
        dosage = " — ".join([p for p in [dur, freq] if p])
        print(f"  {i}. {name} ({url})" + (f" [{dosage}]" if dosage else ""))
    print(f"- Sources: {row['source_urls']}")

def run_interview(csv_path: str):
    df = load_data(csv_path)
    # Ask the parent's reported chronological age
    while True:
        try:
            age_input = input("How old is your baby (in months)? ").strip()
            start_age = int(age_input)
            if start_age < 0 or start_age > 120:
                raise ValueError
            break
        except ValueError:
            print("Please enter a valid age in whole months (e.g., 6, 12, 24).")

    results = {}

    for cat in CATEGORIES:
        rep = pick_representative_rows(df, cat)
        if rep.empty:
            print(f"\n(No data available for category: {cat})")
            continue

        # Clamp starting age between min and max of available ages for the category
        cat_min = rep["max_age_months"].min()
        cat_max = rep["max_age_months"].max()
        start_for_cat = min(max(start_age, cat_min), cat_max)

        print("\n" + "#"*72)
        print(f"Category: {cat.replace('_',' ').title()}")
        level = find_functional_level_for_category(rep, start_for_cat)
        results[cat] = (level, rep)

    print("\n" + "="*72)
    print("Summary: Estimated functional levels by category")
    for cat, (level, _) in results.items():
        print(f" - {cat.replace('_',' ').title()}: ~{level} months")

    print("\nRecommendations by category")
    for cat, (level, rep) in results.items():
        print_category_summary(cat, rep, level)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GeneX Milestone Interviewer")
    parser.add_argument("--csv", type=str, default="genex_milestone_therapy_100_fixed.csv",
                        help="Path to the milestone→therapy CSV (fixed schema).")
    args = parser.parse_args()
    try:
        run_interview(args.csv)
    except FileNotFoundError:
        print(f"CSV not found at '{args.csv}'. Please pass --csv /path/to/file.csv", file=sys.stderr)
        sys.exit(1)
