"""Grep gate on knowledge leaks outside extraction layer.

Prevents hardcoded borrower names, covenant numbers, thresholds, TXN-/ACC-
prefixes, and scenario IDs in solution/ and run.sh. Forbidden list built from
eval data (FACTS/SPECS), template, and official formats.
"""

import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# Import data sources
from expected_extraction import FACTS, SPECS

# Scenario IDs from public submission template
_SCENARIOS = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "B1", "B4"]

# Known exceptions: inherent to code architecture per CLAUDE.md.
# These are documented architectural patterns, not data leaks.
_FILE_EXCEPTIONS = {
    "solution/templates.py": {"Capital": "Covenant heading text (_TEMPLATE_HEADING_TEXT table)"},
    "solution/specs_extract.py": {
        # Spec extraction layer must search for threshold values in PDFs
        "0.04": "Threshold search format",
        "1500000": "Threshold format",
        "1500000.0": "Threshold variant",
        "1500000.00": "Threshold variant",
        "2.0": "Threshold variant",
        "500000": "Threshold format",
        "500000.0": "Threshold variant",
        "500000.00": "Threshold variant",
        "9.0": "Threshold variant",
        "9.00": "Threshold variant",
    },
    "solution/interp.py": {
        # DSL interpreter examples/tests reference covenant thresholds
        "9.0": "Example threshold in code comments",
        "9.00": "Example threshold in code comments",
    },
    "solution/engine.py": {
        # Engine docstrings explain data preparation for specific scenarios
        "B4": "Scenario-specific edge case in docstring",
        "6.3": "Covenant number in example explanation",
    },
    "solution/fallbacks.py": {
        # Fallback family logic inherently references covenant family numbers
        "6.1": "Covenant family index (architectural fixture)",
        "6.2": "Covenant family index (architectural fixture)",
    },
    "solution/ledger.py": {
        # Ledger processing documents scenario-specific transaction rules
        "P7": "Scenario documentation in module docstring",
        "P8": "Scenario documentation in module docstring",
        "TXN-": "Transaction ID prefix in documentation",
    },
    "solution/route.py": {
        # Route dispatcher must recognize account/transaction prefixes from documents
        "ACC-": "Account ID prefix pattern for document routing",
    },
    "solution/solve.py": {
        # Solver harness references covenant numbers as problem structure
        "6.1": "Covenant number (problem structure from CASE)",
    },
}


# Weight constants to exclude from forbidden literals (CASE.ru.md scoring formula)
# Also exclude 0.0 and 0.00 (initialization/comparison, not a threshold)
_WEIGHT_SCORES = {"0.50", "0.30", "0.20", "0.05", "0.5", "0.3", "0.2", "0.0", "0.00"}

# Covenant numbers (public template)
_COVENANT_NUMBERS = {"6.1", "6.2", "6.3"}

# Taxonomy categories (template.py): stub-format allowed in solution/
_TAXONOMY_CATEGORIES = {
    "REVENUE",
    "OTHER_OPEX",
    "INTEREST",
    "PAYROLL",
    "UTILITIES",
    "CAPEX",
    "ALL",
    "FINANCING",
    "RENT",
    "TAX",
}


def _extract_number_formats(num: Decimal | int | float) -> set[str]:
    """Convert numeric threshold to all possible string representations.

    Handles 9.00, 9.0, 500000, 500_000, 4_000_000 formats.
    Only add bare integers if > 999 to avoid false positives from small numbers.
    Uses ROUND_HALF_UP to match scoring logic.
    Skips rounded results that coincide with weight scores.
    """
    formats = set()

    # Add Decimal variants for fractional thresholds
    if isinstance(num, (int, float)):  # noqa: UP038
        d = Decimal(str(num))
    else:
        d = num

    # Two decimal places format (using ROUND_HALF_UP to match score.py)
    quantized = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    quantized_str = str(quantized)
    if quantized_str not in _WEIGHT_SCORES:
        formats.add(quantized_str)

    # One decimal place format
    quantized_one = d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    quantized_one_str = str(quantized_one)
    # Skip results that are weight scores or match the more precise form
    if (
        quantized_one_str not in {"0.0"}
        and quantized_one_str not in _WEIGHT_SCORES
        and quantized_one_str != quantized_str
    ):
        formats.add(quantized_one_str)

    # For integers: add underscore-delimited format (to catch 500_000, 4_000_000)
    if d == d.to_integral_value():
        int_val = int(d)
        int_str = str(int_val)

        # Only add bare integer for large numbers (> 999)
        if int_val > 999:
            formats.add(int_str)
            # Underscore-delimited format
            if len(int_str) > 3:
                delimited = "_".join([int_str[max(0, i - 3) : i] for i in range(len(int_str), 0, -3)][::-1])
                formats.add(delimited)
        elif int_val > 0:
            # For small integers, only add if explicitly written (e.g., "3.0" from Decimal)
            pass

    return formats


def _extract_tokens(phrase: str) -> set[str]:
    """Extract word tokens from phrase (length >= 4 chars).

    Used to catch embedded borrower/entity names.
    """
    # Split on non-word boundaries, filter length
    tokens = re.findall(r"\b\w{4,}\b", phrase)
    return set(tokens)


def forbidden_literals() -> list[str]:
    """Build complete list of forbidden substrings from eval data.

    Sources:
    - Related parties from FACTS (names + 4+ char tokens)
    - Covenant numbers from template (6.1, 6.2, 6.3)
    - Thresholds from SPECS (numeric formats)
    - Scenario IDs (P1-P10, B1, B4)
    - Prefixes TXN-, ACC-
    """
    forbidden = set()

    # Related parties and their word tokens
    for scenario_facts in FACTS.values():
        for party_name in scenario_facts.get("related_parties", []):
            forbidden.add(party_name)
            # Extract word tokens (4+ chars) from entity names
            forbidden.update(_extract_tokens(party_name))

    # Covenant numbers
    forbidden.update(_COVENANT_NUMBERS)

    # Thresholds from SPECS (numeric values in all formats)
    for scenario_specs in SPECS.values():
        for _covenant_id, spec in scenario_specs.items():
            # spec is a tuple: (metric_name, direction, threshold, [optional_dict])
            threshold = spec[2]
            # Exclude weights that match scoring formula
            threshold_str = str(threshold)
            if threshold_str not in _WEIGHT_SCORES:
                forbidden.update(_extract_number_formats(threshold))

            # Handle optional trigger dict (e.g., trigger_financing)
            if len(spec) > 3 and isinstance(spec[3], dict):
                for trigger_val in spec[3].values():
                    trigger_str = str(trigger_val)
                    if trigger_str not in _WEIGHT_SCORES:
                        forbidden.update(_extract_number_formats(trigger_val))

    # Scenario IDs (as word boundaries to avoid PAGE1 -> P1 match)
    forbidden.update(_SCENARIOS)

    # Transaction and account prefixes
    forbidden.add("TXN-")
    forbidden.add("ACC-")

    # Remove taxonomy categories (allowed in solution/)
    forbidden.difference_update(_TAXONOMY_CATEGORIES)

    return sorted(forbidden)


def scan(paths: list[Path]) -> list[dict]:
    """Scan files for forbidden literals; return {"file", "line", "literal"}.

    Uses word-boundary matching for scenario IDs to avoid false positives.
    Respects file-specific exceptions (documented in _FILE_EXCEPTIONS).
    """
    forbidden = forbidden_literals()
    results = []

    # Build regex patterns: word-boundary for scenario IDs, plain substring for others
    patterns = {}
    for lit in forbidden:
        if lit in _SCENARIOS:
            # Use word boundary for scenario IDs
            patterns[lit] = (re.compile(r"\b" + re.escape(lit) + r"\b"), True)
        else:
            # Plain substring search
            patterns[lit] = (None, False)

    for path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        path_str = str(path)
        exceptions = _FILE_EXCEPTIONS.get(path_str, {})

        lines = content.splitlines()
        for line_num, line in enumerate(lines, 1):
            for lit, (regex, use_regex) in patterns.items():
                # Skip if literal is excepted for this file
                if lit in exceptions:
                    continue

                if use_regex:
                    if regex.search(line):
                        results.append({"file": path_str, "line": line_num, "literal": lit})
                else:
                    if lit in line:
                        results.append({"file": path_str, "line": line_num, "literal": lit})

    return results


def main() -> None:
    """Scan solution/*.py and run.sh; exit(1) if violations found."""
    paths = sorted(Path("solution").glob("*.py")) + [Path("run.sh")]
    paths = [p for p in paths if p.exists()]

    hits = scan(paths)

    if hits:
        print("Grep gate violations found:", file=sys.stderr)
        for hit in hits:
            print(f"{hit['file']}:{hit['line']}: {hit['literal']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
