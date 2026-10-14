"""Keep the published evaluation summary and local links accurate."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
RESULTS = json.loads((ROOT / "evals/results/eval-20261011T211353Z.json").read_text())


def test_the_measured_numbers_in_the_readme_match_the_saved_run():
    one, two, three = RESULTS["criteria"].values()
    assert f"{one['paid']} of {one['fixable_cases_expected']} fixable cases" in README
    assert f"{two['escalated_with_an_accurate_note']} of {two['other_cases']} other cases" in README
    assert f"median {three['median']} seconds, max {three['max']} seconds" in README
    assert f"Message accuracy: {RESULTS['message_accuracy']:.0%}" in README
    assert f"Message quality: {RESULTS['message_quality']:.1f} of 5" in README
    assert f"{RESULTS['cases']} real sandbox cases" in README
    assert f"evals/results/{RESULTS['run_id']}.json" in README
    assert RESULTS["provider_model"].split(":")[1] in README


def test_every_local_file_the_readme_links_to_exists():
    for target in re.findall(r"\]\((?!https?://)([^)#]+)", README):
        assert (ROOT / target).is_file(), target
