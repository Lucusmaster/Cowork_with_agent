"""关网页自动收工：把判定逻辑抽成函数后，直接测逻辑（不用起服务）。"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_team import room_web


class GuardianTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(room_web.PRESENCE)
        room_web.PRESENCE.update({"last_seen": 0.0, "bye_at": 0.0,
                                  "ever": False, "deferred": False})
        self.now = 1000.0

    def tearDown(self) -> None:
        room_web.PRESENCE.clear()
        room_web.PRESENCE.update(self._saved)

    def trigger(self, idle: float = 90.0, grace: float = 5.0) -> str:
        return room_web._guardian_trigger(idle, grace, now=self.now)

    def test_never_opened_does_not_shutdown(self) -> None:
        room_web.PRESENCE.update({"last_seen": 0.0, "bye_at": 0.0, "ever": False})
        self.assertEqual(self.trigger(), "", "命令行起的服务不该自己退出")

    def test_page_alive_keeps_running(self) -> None:
        room_web.PRESENCE.update({"ever": True, "last_seen": self.now - 2, "bye_at": 0.0})
        self.assertEqual(self.trigger(), "")

    def test_bye_fires_after_grace(self) -> None:
        room_web.PRESENCE.update({"ever": True, "last_seen": self.now - 6, "bye_at": self.now - 6})
        self.assertEqual(self.trigger(idle=90, grace=5), "网页已关闭")

    def test_bye_within_grace_waits_for_refresh(self) -> None:
        room_web.PRESENCE.update({"ever": True, "last_seen": self.now - 2, "bye_at": self.now - 2})
        self.assertEqual(self.trigger(idle=90, grace=5), "", "2 秒内的刷新不该被当成关闭")

    def test_idle_timeout_without_bye(self) -> None:
        room_web.PRESENCE.update({"ever": True, "last_seen": self.now - 95, "bye_at": 0.0})
        self.assertEqual(self.trigger(idle=90, grace=5), "网页已关闭（长时间没有心跳）")

    def test_stale_page_but_fresh_bye_still_waits_idle(self) -> None:
        # 页面还活着（last_seen 很新）但误发了 bye：grace 过了、idle 没到 grace，不该关
        room_web.PRESENCE.update({"ever": True, "last_seen": self.now - 1, "bye_at": self.now - 10})
        self.assertEqual(self.trigger(idle=90, grace=5), "")

    def test_refresh_clears_bye(self) -> None:
        room_web.PRESENCE.update({"ever": True, "last_seen": self.now - 30, "bye_at": self.now - 30})
        self.assertEqual(self.trigger(idle=90, grace=5), "网页已关闭")
        room_web.PRESENCE.update({"bye_at": 0.0, "last_seen": self.now})   # 页面回来了
        self.assertEqual(self.trigger(idle=90, grace=5), "")


class GuardianTickTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(room_web.PRESENCE)
        room_web.PRESENCE.update({"last_seen": 0.0, "bye_at": 0.0,
                                  "ever": True, "deferred": False})
        self.now = 1000.0

    def tearDown(self) -> None:
        room_web.PRESENCE.clear()
        room_web.PRESENCE.update(self._saved)

    def _gone(self) -> None:
        room_web.PRESENCE.update({"last_seen": self.now - 60, "bye_at": self.now - 60})
        patcher = mock.patch.object(room_web.time, "time", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_active_tasks_defer_shutdown_and_warn_once(self) -> None:
        self._gone()
        with mock.patch.object(room_web, "_active_tasks", return_value=["T-1", "T-2"]), \
             mock.patch.object(room_web, "room") as fake_room:
            self.assertEqual(room_web._guardian_tick(90, 5), "", "任务没完就不收工")
            self.assertTrue(room_web.PRESENCE["deferred"])
            self.assertEqual(fake_room.post.call_count, 1, "只提示一次")
            text = fake_room.post.call_args[0][1]
            self.assertIn("2 个任务", text)
            self.assertEqual(room_web._guardian_tick(90, 5), "")
            self.assertEqual(fake_room.post.call_count, 1, "第二次不再重复提示")

    def test_no_active_tasks_shuts_down(self) -> None:
        self._gone()
        with mock.patch.object(room_web, "_active_tasks", return_value=[]):
            self.assertEqual(room_web._guardian_tick(90, 5), "网页已关闭")

    def test_page_alive_no_shutdown(self) -> None:
        room_web.PRESENCE.update({"last_seen": self.now, "bye_at": 0.0})
        with mock.patch.object(room_web.time, "time", return_value=self.now), \
             mock.patch.object(room_web, "_active_tasks", return_value=["T-1"]):
            self.assertEqual(room_web._guardian_tick(90, 5), "")


if __name__ == "__main__":
    unittest.main()