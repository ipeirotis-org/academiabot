"""
Evaluation harness for multi-model university school extraction.

Usage:
    python -m wikidata_discover.eval.run_eval
    python -m wikidata_discover.eval.run_eval --providers openai anthropic
    python -m wikidata_discover.eval.run_eval --universities Q49210 Q49088
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from rich.console import Console
from rich.table import Table

from wikidata_discover.eval.ground_truth import GROUND_TRUTH
from wikidata_discover.llm_helpers import LLMHelper
from wikidata_discover.discovery import is_fuzzy_match

console = Console()
EVAL_DIR = Path(__file__).parent

PROVIDER_EXTRACTORS = {
    "openai": LLMHelper.extract_divisions_openai,
    "anthropic": LLMHelper.extract_divisions_anthropic,
    "gemini": LLMHelper.extract_divisions_gemini,
}

PROVIDER_KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}


def get_available_providers() -> List[str]:
    available = []
    for provider, env_var in PROVIDER_KEY_VARS.items():
        if os.getenv(env_var):
            available.append(provider)
        else:
            console.print(f"[yellow]Skipping {provider}: {env_var} not set[/yellow]")
    return available


def compute_metrics(
    predicted: List[str], ground_truth: List[str]
) -> Tuple[float, float, float]:
    if not predicted and not ground_truth:
        return 1.0, 1.0, 1.0
    if not predicted:
        return 0.0, 0.0, 0.0
    if not ground_truth:
        return 0.0, 0.0, 0.0

    # Greedy one-to-one match: each predicted item can satisfy at most one
    # ground-truth item (and vice versa). Without this, the same predicted
    # name could be counted against multiple truth entries and inflate recall.
    used_truth: set = set()
    tp = 0
    for p in predicted:
        for j, g in enumerate(ground_truth):
            if j in used_truth:
                continue
            if is_fuzzy_match(p, g):
                used_truth.add(j)
                tp += 1
                break

    precision = tp / len(predicted)
    recall = tp / len(ground_truth)
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return precision, recall, f1


def extract_names(divisions: List[Dict]) -> List[str]:
    names = []
    for d in divisions:
        name = d.get("name") or d.get("unit")
        if name:
            names.append(name)
    return names


def union_names(list_a: List[str], list_b: List[str]) -> List[str]:
    seen = []
    for name in list_a + list_b:
        if not any(is_fuzzy_match(name, s) for s in seen):
            seen.append(name)
    return seen


def run_eval(providers: List[str], university_qids: List[str]) -> pd.DataFrame:
    rows = []

    for qid in university_qids:
        info = GROUND_TRUTH[qid]
        univ_name = info["name"]
        website = info["website"]
        truth = info["schools"]

        console.print(f"\n[bold blue]== {univ_name} ({qid}) ==[/bold blue]")

        # Extract per provider
        provider_names: Dict[str, List[str]] = {}
        for provider in providers:
            console.print(f"  [dim]Extracting with {provider}...[/dim]")
            try:
                extractor = PROVIDER_EXTRACTORS[provider]
                divisions = extractor(univ_name, website)
                names = extract_names(divisions)
                provider_names[provider] = names
                p, r, f = compute_metrics(names, truth)
                console.print(f"  {provider}: {len(names)} schools, P={p:.3f} R={r:.3f} F1={f:.3f}")
                rows.append({
                    "university": univ_name,
                    "qid": qid,
                    "method": f"single_{provider}",
                    "n_predicted": len(names),
                    "n_truth": len(truth),
                    "precision": round(p, 4),
                    "recall": round(r, 4),
                    "f1": round(f, 4),
                })
            except Exception as e:
                console.print(f"  [red]{provider} failed: {e}[/red]")
                provider_names[provider] = []

        # Rotating judge combos -- both with and without web search
        all_providers = list(provider_names.keys())
        if len(all_providers) >= 2:
            for judge in all_providers:
                generators = [p for p in all_providers if p != judge]
                gen_lists = [provider_names[g] for g in generators]
                if len(gen_lists) == 1:
                    u = gen_lists[0]
                else:
                    u = union_names(gen_lists[0], gen_lists[1])

                if not u:
                    continue

                combo_label = f"judge_{judge}_gen_{'_'.join(generators)}"
                console.print(f"  [dim]{combo_label} ({len(u)} items)...[/dim]")
                try:
                    kept = LLMHelper.judge_union(univ_name, u, judge)
                    p, r, f = compute_metrics(kept, truth)
                    console.print(f"  {combo_label}: {len(kept)} kept, P={p:.3f} R={r:.3f} F1={f:.3f}")
                    rows.append({
                        "university": univ_name,
                        "qid": qid,
                        "method": combo_label,
                        "n_predicted": len(kept),
                        "n_truth": len(truth),
                        "precision": round(p, 4),
                        "recall": round(r, 4),
                        "f1": round(f, 4),
                    })
                except Exception as e:
                    console.print(f"  [red]{combo_label} failed: {e}[/red]")

    return pd.DataFrame(rows)


def print_summary_table(df: pd.DataFrame):
    summary = df.groupby("method")[["precision", "recall", "f1"]].mean().round(4)
    summary["n_universities"] = df.groupby("method")["qid"].nunique()
    summary = summary.sort_values("f1", ascending=False)

    table = Table(
        title="Evaluation Summary (averaged across universities)",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Method")
    table.add_column("Avg Precision", justify="right")
    table.add_column("Avg Recall", justify="right")
    table.add_column("Avg F1", justify="right")
    table.add_column("Universities", justify="right")

    for method, row in summary.iterrows():
        table.add_row(
            method,
            f"{row['precision']:.4f}",
            f"{row['recall']:.4f}",
            f"{row['f1']:.4f}",
            str(int(row["n_universities"])),
        )

    console.print(table)


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate multi-model university school extraction"
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=["openai", "anthropic", "gemini"],
        help="Providers to use (default: all with keys set)",
    )
    parser.add_argument(
        "--universities",
        nargs="+",
        help="QIDs to evaluate (default: all in ground truth)",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    providers = args.providers or get_available_providers()
    if not providers:
        console.print("[red]No providers available. Set at least one API key.[/red]")
        sys.exit(1)

    university_qids = args.universities or list(GROUND_TRUTH.keys())
    invalid = [q for q in university_qids if q not in GROUND_TRUTH]
    if invalid:
        console.print(f"[red]Unknown QIDs: {invalid}[/red]")
        sys.exit(1)

    console.print(f"[bold]Providers: {providers}[/bold]")
    console.print(f"[bold]Universities: {len(university_qids)}[/bold]")

    df = run_eval(providers, university_qids)

    # Save results
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    per_univ_path = EVAL_DIR / "results_per_university.csv"
    summary_path = EVAL_DIR / "results_summary.csv"

    df.to_csv(per_univ_path, index=False)
    summary = df.groupby("method")[["precision", "recall", "f1"]].mean().round(4)
    summary.to_csv(summary_path)

    console.print(f"\n[green]Saved per-university results to {per_univ_path}[/green]")
    console.print(f"[green]Saved summary to {summary_path}[/green]")

    print_summary_table(df)


if __name__ == "__main__":
    main()
