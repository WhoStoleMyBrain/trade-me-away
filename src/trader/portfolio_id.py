"""Read a Coinbase key's portfolio UUID without loading trader configuration."""

import argparse
import getpass
import json
import logging
import sys
import warnings
from pathlib import Path
from uuid import UUID

from coinbase.rest import RESTClient


def _credentials(key_file: Path | None) -> tuple[str, str]:
    if key_file is not None:
        data = json.loads(key_file.read_text(encoding="utf-8-sig"))
        name, secret = data["name"], data["privateKey"]
    else:
        # Refuse getpass's fallback to visible input when no secure terminal exists.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            name = getpass.getpass("Coinbase API key name (hidden): ")
            secret = getpass.getpass("Private key (hidden; one line with literal \\n): ")
    if not isinstance(name, str) or not isinstance(secret, str):
        raise ValueError("Invalid credentials")
    name, secret = name.strip(), secret.strip().replace("\\n", "\n")
    if not name or not secret or "REPLACE_" in name or "REPLACE_" in secret:
        raise ValueError("Missing credentials")
    return name, secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-file",
        type=Path,
        help="Downloaded Coinbase JSON key file containing name and privateKey",
    )
    args = parser.parse_args(argv)
    # The SDK logs raw HTTP error bodies even with verbose=False. This standalone
    # command suppresses logging while handling credentials and the single GET.
    previous_logging_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        name, secret = _credentials(args.key_file)
        client = RESTClient(api_key=name, api_secret=secret, timeout=15, verbose=False)
        permissions = client.get_api_key_permissions().to_dict()
        if permissions.get("can_view") is not True:
            raise ValueError("View permission required")
        portfolio = permissions.get("portfolio_uuid")
        if not isinstance(portfolio, str):
            raise ValueError("Missing portfolio UUID")
        portfolio_id = str(UUID(portfolio))
    except (KeyboardInterrupt, EOFError):
        print("Portfolio lookup cancelled.", file=sys.stderr)
        return 130
    except Exception:
        # Never expose SDK exceptions, credentials, headers, or raw responses.
        print(
            "Portfolio lookup failed. Check the key name/private key, View permission, "
            "key file format, VM IP allowlist, system clock, and network access.",
            file=sys.stderr,
        )
        return 1
    finally:
        logging.disable(previous_logging_threshold)
    print(portfolio_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
