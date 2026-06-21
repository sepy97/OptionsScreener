"""Daily job: download the CBOE 'Available Weeklys' CSV and refresh the has_weeklys set.

Source: https://www.cboe.com/available_weeklys/get_csv_download/ (no header row; mixed
sections; updated each business day).

TODO(M4): fetch, parse defensively, persist the ticker set.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("CBOE weeklys refresh lands in M4")


if __name__ == "__main__":
    main()
