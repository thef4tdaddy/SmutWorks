#!/usr/bin/env python3
"""Public SmutWorks command; import the app after resolving directory overrides."""
import argparse
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description='SmutWorks — Stash media organization and encoding toolkit.')
    parser.add_argument('--version', action='version', version='SmutWorks 0.1.0-alpha.2')
    parser.add_argument('--config-dir', type=Path, help='Private settings/cache directory (or SMUTWORKS_CONFIG_DIR).')
    parser.add_argument('--reports-dir', type=Path, help='Reports/history directory (or SMUTWORKS_REPORTS_DIR).')
    args = parser.parse_args(argv)
    for value, variable in ((args.config_dir, 'SMUTWORKS_CONFIG_DIR'), (args.reports_dir, 'SMUTWORKS_REPORTS_DIR')):
        if value is not None:
            os.environ[variable] = str(value.expanduser().resolve())
    from adult_content_organizer import main as run
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
