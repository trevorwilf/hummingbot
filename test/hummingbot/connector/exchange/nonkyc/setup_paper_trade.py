"""
NonKYC Paper Trade Setup Helper
================================
Checks and optionally configures paper trading for NonKYC.

Usage:
  python test/hummingbot/connector/exchange/nonkyc/setup_paper_trade.py          # check only
  python test/hummingbot/connector/exchange/nonkyc/setup_paper_trade.py --apply  # auto-patch conf
"""
import os
import sys
from pathlib import Path


def find_conf_client():
    """Search common locations for conf_client.yml."""
    candidates = []
    # Walk up from this file to find repo root
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists() or (current / ".env").exists():
            break
        current = current.parent

    repo_root = current
    search_paths = [
        repo_root / "conf" / "conf_client.yml",
        repo_root / "hummingbot" / "conf" / "conf_client.yml",
        repo_root / "conf_client.yml",
        Path.home() / ".hummingbot" / "conf" / "conf_client.yml",
    ]

    for p in search_paths:
        if p.exists():
            candidates.append(p)

    return candidates, repo_root


def check_config(path):
    """Check if conf_client.yml has nonkyc in paper_trade_exchanges."""
    content = path.read_text()
    has_nonkyc = "nonkyc" in content and "paper_trade_exchanges" in content
    has_sal = "SAL" in content

    # Simple check: find lines after paper_trade_exchanges that contain "- nonkyc"
    lines = content.split("\n")
    in_paper_trade = False
    nonkyc_in_list = False
    for line in lines:
        stripped = line.strip()
        if "paper_trade_exchanges" in stripped:
            in_paper_trade = True
            continue
        if in_paper_trade:
            if stripped.startswith("- "):
                if stripped == "- nonkyc":
                    nonkyc_in_list = True
            elif stripped and not stripped.startswith("#"):
                in_paper_trade = False

    return nonkyc_in_list, has_sal, content, lines


def apply_patch(path, content):
    """Add nonkyc to paper_trade_exchanges list."""
    lines = content.split("\n")
    new_lines = []
    in_paper_trade = False
    inserted = False

    for line in lines:
        stripped = line.strip()
        if "paper_trade_exchanges" in stripped:
            in_paper_trade = True
            new_lines.append(line)
            continue

        if in_paper_trade and not inserted:
            if stripped.startswith("- "):
                new_lines.append(line)
                # Check if next line is still a list item
                continue
            else:
                # End of list - insert nonkyc before this line
                # Detect indentation from previous list items
                for prev in reversed(new_lines):
                    if prev.strip().startswith("- "):
                        indent = prev[:len(prev) - len(prev.lstrip())]
                        new_lines.append(f"{indent}- nonkyc")
                        inserted = True
                        break
                in_paper_trade = False

        if in_paper_trade and stripped.startswith("- "):
            new_lines.append(line)
            continue

        new_lines.append(line)

    if not inserted:
        # Fallback: append after finding the section
        print("  Could not auto-patch. Please add manually.")
        return False

    path.write_text("\n".join(new_lines))
    return True


def main():
    apply = "--apply" in sys.argv

    print()
    print("=" * 60)
    print("  NonKYC Paper Trade Setup Helper")
    print("=" * 60)
    print()

    candidates, repo_root = find_conf_client()

    if not candidates:
        print("  [WARN] conf_client.yml not found in common locations.")
        print(f"  Searched from: {repo_root}")
        print()
        print("  To set up paper trading manually:")
        print("  1. Open your conf_client.yml")
        print("  2. Find the paper_trade section")
        print("  3. Add 'nonkyc' to paper_trade_exchanges list")
        print()
    else:
        for conf_path in candidates:
            print(f"  Found: {conf_path}")
            nonkyc_in_list, has_sal, content, lines = check_config(conf_path)

            if nonkyc_in_list:
                print("  [OK] 'nonkyc' is in paper_trade_exchanges")
            else:
                print("  [MISSING] 'nonkyc' is NOT in paper_trade_exchanges")
                if apply:
                    print("  Applying patch...")
                    if apply_patch(conf_path, content):
                        print("  [OK] Added 'nonkyc' to paper_trade_exchanges")
                    else:
                        print("  [FAIL] Could not auto-patch")
                else:
                    print("  Run with --apply to auto-add, or add manually")

            if has_sal:
                print("  [OK] 'SAL' found in config (paper_trade_account_balance)")
            else:
                print("  [INFO] 'SAL' not in config (optional)")
            print()

    print()
    print("=" * 60)
    print("  Quick Start -- Paper Trading with NonKYC")
    print("=" * 60)
    print()
    print("  1. Ensure 'nonkyc' is in paper_trade_exchanges in conf_client.yml")
    print()
    print("  2. In Hummingbot CLI:")
    print("     connect nonkyc_paper_trade")
    print()
    print("  3. Create a strategy:")
    print("     create --script directional_strategy_rsi.py")
    print()
    print("  4. When prompted for exchange: nonkyc_paper_trade")
    print("     When prompted for pair: BTC-USDT")
    print()
    print("  5. Start:")
    print("     start")
    print()
    print("  Paper trading uses REAL market data from NonKYC")
    print("  but simulates order placement and fills locally.")
    print("  No API keys are needed for paper trading.")
    print()


if __name__ == "__main__":
    main()
