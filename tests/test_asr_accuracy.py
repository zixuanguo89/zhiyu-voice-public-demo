import json
import unittest
from pathlib import Path

from evals.asr_accuracy import score

CASES = {case["id"]: case for case in json.loads((Path(__file__).resolve().parents[1] /
         "fixtures" / "asr_accuracy_cases.json").read_text(encoding="utf-8"))["cases"]}


class SpeechScoringCases(unittest.TestCase):
    def test_equivalent_number_format_passes_but_inserted_digit_fails(self):
        case = CASES["number-02"]
        correct = score(case, "我的预约编号是 ０６０９３７，开头的零也要保留。")
        self.assertTrue(correct["critical_pass"])
        self.assertEqual(correct["field_normalized_edits"], 0)
        self.assertFalse(score(case, "我的预约编号是 0609377，开头的零也要保留。")["critical_pass"])

    def test_negated_confirmation_is_not_scored_as_confirmation(self):
        self.assertFalse(score(CASES["control-03"], "对，这个时间可以，我不确认预约")["critical_pass"])
        self.assertFalse(score(CASES["negation-01"], "转人工，我只想确认预约时间。")["critical_pass"])

    def test_correction_sequence_matters(self):
        case = CASES["correction-02"]
        self.assertTrue(score(case, "尾号是 5863。不对，尾号是 5864。")["critical_pass"])
        self.assertFalse(score(case, "尾号是 5864。不对，尾号是 5863。")["critical_pass"])


if __name__ == "__main__":
    unittest.main()
