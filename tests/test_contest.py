import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _stamp(offset_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class ContestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "contest.db"),
            RuleEngine(),
        )
        self.analyst = Actor("analyst-1", "analyst")
        self.coordinator = Actor("coordinator-1", "coordinator")
        self.operator = Actor("operator-1", "operator")
        self.telescope = self.service.create(
            self.coordinator,
            "telescope",
            {"name": "North 2m", "aperture_m": 2.0, "site_name": "NAO"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate(self, event_id, magnitude, transient_type="supernova"):
        source = self.service.create(
            self.analyst,
            "source",
            {"name": "Survey " + event_id, "survey_name": "ZTF"},
        )
        return self.service.create(
            self.analyst,
            "candidate",
            {
                "source_id": source["id"],
                "event_id": event_id,
                "ra": 120.5,
                "dec": -12.25,
                "magnitude": magnitude,
                "transient_type": transient_type,
                "observed_at": _stamp(-24),
            },
        )

    def _request(self, candidate, team, start_off, end_off, mode="imaging"):
        return self.service.create(
            self.analyst,
            "observation",
            {
                "candidate_id": candidate["id"],
                "telescope_id": self.telescope["id"],
                "team_id": team,
                "start_at": _stamp(start_off),
                "end_at": _stamp(end_off),
                "mode": mode,
            },
        )

    def _schedule(self, observation):
        return self.service.transition(
            self.coordinator, observation["id"], "schedule", {"operator_id": "op-1"}
        )

    def test_higher_priority_bumps_lower_priority_plan(self):
        faint = self._candidate("AT-low", 19.0)
        bright = self._candidate("AT-high", 12.0)
        low = self._request(faint, "team-a", 6, 8)
        high = self._request(bright, "team-b", 7, 9)
        self.assertGreater(high["data"]["priority"], low["data"]["priority"])

        self._schedule(low)
        result = self._schedule(high)

        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["preempted"], [low["id"]])
        bumped = self.service.get(low["id"])
        self.assertEqual(bumped["status"], "requested")
        self.assertIn(high["id"], bumped["data"]["preempt_reason"])
        self.assertIn("顶回待排", bumped["data"]["preempt_reason"])
        self.assertEqual(bumped["data"]["preempted_by"], high["id"])
        self.assertNotIn("scheduled_by", bumped["data"])

        audit = self.service.audit_log(low["id"])
        self.assertEqual(audit[-1]["action"], "preempt")
        self.assertEqual(audit[-1]["from_status"], "scheduled")
        self.assertEqual(audit[-1]["to_status"], "requested")

    def test_lower_priority_cannot_bump(self):
        faint = self._candidate("AT-low-2", 19.0)
        bright = self._candidate("AT-high-2", 12.0)
        high = self._request(bright, "team-c", 10, 12)
        low = self._request(faint, "team-d", 11, 13)
        self._schedule(high)
        with self.assertRaises(ConflictError):
            self._schedule(low)
        # Both keep their state.
        self.assertEqual(self.service.get(high["id"])["status"], "scheduled")
        self.assertEqual(self.service.get(low["id"])["status"], "requested")

    def test_same_priority_keeps_earlier_submission(self):
        first = self._candidate("AT-first", 17.0)
        second = self._candidate("AT-second", 17.0)
        earlier = self._request(first, "team-e", 14, 16)
        later = self._request(second, "team-f", 15, 17)
        self.assertEqual(earlier["data"]["priority"], later["data"]["priority"])
        self.assertLess(earlier["seq"], later["seq"])

        self._schedule(earlier)
        with self.assertRaises(ConflictError):
            self._schedule(later)
        self.assertEqual(self.service.get(later["id"])["status"], "requested")
        self.assertIsNone(self.service.get(later["id"])["data"].get("preempt_reason"))

    def test_started_plan_cannot_be_bumped(self):
        faint = self._candidate("AT-started-low", 19.0)
        bright = self._candidate("AT-started-high", 12.0)
        running = self._request(faint, "team-g", -1, 2)
        challenger = self._request(bright, "team-h", 0, 1)
        self._schedule(running)
        with self.assertRaises(ConflictError) as raised:
            self._schedule(challenger)
        self.assertIn("already started", str(raised.exception))
        self.assertEqual(self.service.get(running["id"])["status"], "scheduled")

    def test_completed_window_can_be_rescheduled(self):
        faint = self._candidate("AT-done", 19.0)
        bright = self._candidate("AT-next", 12.0)
        done = self._request(faint, "team-i", 20, 22)
        self._schedule(done)
        self.service.transition(
            self.operator, done["id"], "complete", {"quality": "good"}
        )
        follower = self._request(bright, "team-j", 20, 22)
        scheduled = self._schedule(follower)
        self.assertEqual(scheduled["status"], "scheduled")

    def test_withdrawn_window_can_be_rescheduled(self):
        faint = self._candidate("AT-pulled", 19.0)
        bright = self._candidate("AT-replacement", 12.0)
        pulled = self._request(faint, "team-k", 24, 26)
        self._schedule(pulled)
        self.service.transition(
            self.analyst, pulled["id"], "withdraw", {"reason": "weather"}
        )
        replacement = self._request(bright, "team-l", 24, 26)
        self.assertEqual(self._schedule(replacement)["status"], "scheduled")

    def test_bumped_plan_can_be_rescheduled_and_clears_reason(self):
        faint = self._candidate("AT-bumped", 19.0)
        bright = self._candidate("AT-bump", 12.0)
        victim = self._request(faint, "team-m", 30, 32)
        winner = self._request(bright, "team-n", 31, 33)
        self._schedule(victim)
        self._schedule(winner)
        # The winner finishes on another night; the victim's original window frees up.
        self.service.transition(
            self.operator, winner["id"], "complete", {"quality": "good"}
        )
        rescheduled = self._schedule(victim)
        self.assertEqual(rescheduled["status"], "scheduled")
        self.assertNotIn("preempt_reason", rescheduled["data"])

    def test_team_double_booking_is_still_rejected(self):
        bright_one = self._candidate("AT-team-1", 13.0)
        bright_two = self._candidate("AT-team-2", 14.0)
        other_scope = self.service.create(
            self.coordinator,
            "telescope",
            {"name": "South 1m", "aperture_m": 1.0, "site_name": "ESO"},
        )
        first = self._request(bright_one, "same-team", 40, 42)
        second_payload = {
            "candidate_id": bright_two["id"],
            "telescope_id": other_scope["id"],
            "team_id": "same-team",
            "start_at": _stamp(40),
            "end_at": _stamp(42),
            "mode": "spectroscopy",
        }
        second = self.service.create(self.analyst, "observation", second_payload)
        self._schedule(first)
        with self.assertRaises(ConflictError):
            self._schedule(second)

    def test_direct_preempt_action_is_rejected(self):
        faint = self._candidate("AT-direct", 19.0)
        plan = self._request(faint, "team-q", 60, 62)
        self._schedule(plan)
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator,
                plan["id"],
                "preempt",
                {"reason": "manual"},
            )

    def test_queue_reports_reasons(self):
        faint = self._candidate("AT-queue-low", 19.0)
        bright = self._candidate("AT-queue-high", 12.0)
        low = self._request(faint, "team-o", 48, 50)
        high = self._request(bright, "team-p", 49, 51)
        self._schedule(low)
        self._schedule(high)

        queue = self.service.queue()
        by_id = {item["id"]: item for item in queue}
        self.assertEqual(by_id[high["id"]]["status"], "scheduled")
        self.assertIsNone(by_id[high["id"]]["queue_reason"])
        self.assertEqual(by_id[low["id"]]["status"], "requested")
        self.assertIn("顶回待排", by_id[low["id"]]["queue_reason"])
        self.assertEqual(by_id[low["id"]]["preempted_by"], high["id"])
        self.assertEqual(by_id[low["id"]]["telescope_name"], "North 2m")


if __name__ == "__main__":
    unittest.main()
