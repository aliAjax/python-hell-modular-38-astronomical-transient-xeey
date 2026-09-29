import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SchedulingPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "scheduling.db"),
            RuleEngine(),
        )
        self.analyst = Actor("analyst-1", "analyst")
        self.coordinator = Actor("coordinator-1", "coordinator")
        self.operator = Actor("operator-1", "operator")
        self.source = self.service.create(
            self.analyst,
            "source",
            {"name": "Survey", "survey_name": "Nightly Survey"},
        )
        self.telescope = self._telescope("North 2m")

    def tearDown(self):
        self.tmp.cleanup()

    def _telescope(self, name):
        return self.service.create(
            self.coordinator,
            "telescope",
            {"name": name, "aperture_m": 2.0, "site_name": "NAO"},
        )

    def _candidate(self, event_id, magnitude=18.0, transient_type="unknown"):
        return self.service.create(
            self.analyst,
            "candidate",
            {
                "source_id": self.source["id"],
                "event_id": event_id,
                "ra": 120.5,
                "dec": -12.25,
                "magnitude": magnitude,
                "transient_type": transient_type,
                "observed_at": "2026-09-29T00:00:00Z",
            },
        )

    def _observation(self, candidate, telescope, team, start_at, end_at):
        return self.service.create(
            self.analyst,
            "observation",
            {
                "candidate_id": candidate["id"],
                "telescope_id": telescope["id"],
                "team_id": team,
                "start_at": start_at,
                "end_at": end_at,
                "mode": "imaging",
            },
        )

    def _schedule(self, observation):
        return self.service.transition(
            self.coordinator,
            observation["id"],
            "schedule",
            {"operator_id": self.operator.user_id},
        )

    def test_higher_priority_preempts_unstarted_lower_priority(self):
        start = "2099-01-01T10:00:00Z"
        end = "2099-01-01T11:00:00Z"
        lower = self._candidate("lower", 19.0, "variable")
        higher = self._candidate("higher", 16.0, "grb")
        low_obs = self._observation(lower, self.telescope, "team-a", start, end)
        high_obs = self._observation(higher, self.telescope, "team-b", start, end)

        self._schedule(low_obs)
        scheduled_high = self._schedule(high_obs)
        bumped_low = self.service.get(low_obs["id"])

        self.assertEqual(scheduled_high["status"], "scheduled")
        self.assertEqual(bumped_low["status"], "requested")
        self.assertIn("顶掉", bumped_low["data"]["queue_reason"])
        self.assertEqual(bumped_low["data"]["bumped_by_observation_id"], high_obs["id"])
        self.assertTrue(
            any(
                item["action"] == "preempt"
                and item["detail"]["replacement_observation_id"] == scheduled_high["id"]
                for item in self.service.audit_log(low_obs["id"])
            )
        )

    def test_same_priority_keeps_earlier_submission(self):
        start = "2099-01-02T10:00:00Z"
        end = "2099-01-02T11:00:00Z"
        first_candidate = self._candidate("same-1", 17.0, "grb")
        second_candidate = self._candidate("same-2", 17.0, "grb")
        first = self._observation(first_candidate, self.telescope, "team-a", start, end)
        second = self._observation(second_candidate, self.telescope, "team-b", start, end)

        self._schedule(first)
        with self.assertRaises(ConflictError):
            self._schedule(second)
        self.assertEqual(self.service.get(first["id"])["status"], "scheduled")
        blocked = self.service.get(second["id"])
        self.assertEqual(blocked["status"], "requested")
        self.assertIn("先提交", blocked["data"]["queue_reason"])
        self.assertTrue(
            any(item["action"] == "schedule_blocked" for item in self.service.audit_log(second["id"]))
        )

    def test_started_observation_cannot_be_preempted(self):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        end = (now + timedelta(hours=1)).isoformat(timespec="seconds")
        lower = self._candidate("started-low", 19.0, "variable")
        higher = self._candidate("waiting-high", 16.0, "grb")
        started = self._observation(lower, self.telescope, "team-a", start, end)
        waiting = self._observation(higher, self.telescope, "team-b", start, end)

        self._schedule(started)
        with self.assertRaises(ConflictError):
            self._schedule(waiting)
        self.assertEqual(self.service.get(started["id"])["status"], "scheduled")
        self.assertEqual(self.service.get(waiting["id"])["status"], "requested")

    def test_blocked_reason_is_cleared_when_later_scheduled(self):
        start = "2099-01-06T10:00:00Z"
        end = "2099-01-06T11:00:00Z"
        first_candidate = self._candidate("blocked-first", 16.0, "grb")
        second_candidate = self._candidate("blocked-second", 17.0, "grb")
        first = self._observation(first_candidate, self.telescope, "team-a", start, end)
        second = self._observation(second_candidate, self.telescope, "team-b", start, end)

        self._schedule(first)
        with self.assertRaises(ConflictError):
            self._schedule(second)
        blocked = self.service.get(second["id"])
        self.assertTrue(blocked["data"]["queue_reason"])
        self.service.transition(self.operator, first["id"], "complete", {"quality": "good"})
        scheduled = self._schedule(second)
        self.assertIsNone(scheduled["data"]["queue_reason"])
        self.assertEqual(scheduled["status"], "scheduled")

    def test_completed_window_can_be_scheduled_again(self):
        start = "2099-01-03T10:00:00Z"
        end = "2099-01-03T11:00:00Z"
        first_candidate = self._candidate("complete-first", 19.0, "variable")
        next_candidate = self._candidate("complete-next", 16.0, "grb")
        first = self._observation(first_candidate, self.telescope, "team-a", start, end)
        second = self._observation(next_candidate, self.telescope, "team-b", start, end)

        self._schedule(first)
        self.service.transition(self.operator, first["id"], "complete", {"quality": "good"})
        self._schedule(second)
        self.assertEqual(self.service.get(second["id"])["status"], "scheduled")

    def test_withdrawn_window_can_be_scheduled_again(self):
        start = "2099-01-05T10:00:00Z"
        end = "2099-01-05T11:00:00Z"
        first_candidate = self._candidate("withdraw-first", 19.0, "variable")
        next_candidate = self._candidate("withdraw-next", 16.0, "grb")
        first = self._observation(first_candidate, self.telescope, "team-a", start, end)
        second = self._observation(next_candidate, self.telescope, "team-b", start, end)

        self._schedule(first)
        self.service.transition(
            self.coordinator, first["id"], "withdraw", {"reason": "天气原因，撤回计划"}
        )
        self._schedule(second)
        self.assertEqual(self.service.get(second["id"])["status"], "scheduled")

    def test_same_team_cannot_be_double_booked_on_another_telescope(self):
        start = "2099-01-04T10:00:00Z"
        end = "2099-01-04T11:00:00Z"
        first_candidate = self._candidate("team-1", 19.0, "variable")
        second_candidate = self._candidate("team-2", 16.0, "grb")
        second_telescope = self._telescope("South 4m")
        first = self._observation(first_candidate, self.telescope, "same-team", start, end)
        second = self._observation(second_candidate, second_telescope, "same-team", start, end)

        self._schedule(first)
        with self.assertRaises(ConflictError):
            self._schedule(second)
        self.assertEqual(self.service.get(second["id"])["status"], "requested")


if __name__ == "__main__":
    unittest.main()
