import subprocess
import sys
from datetime import date

from pipeline import quarter_start


def test_help_runs():
    out = subprocess.run(
        [sys.executable, "-m", "pipeline", "--help"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert "rates" in out.stdout and "fx" in out.stdout


def test_quarter_start():
    assert quarter_start(date(2026, 9, 8)) == date(2026, 7, 1)
    assert quarter_start(date(2026, 10, 1)) == date(2026, 10, 1)
    assert quarter_start(date(2026, 1, 15)) == date(2026, 1, 1)
