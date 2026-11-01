"""Browser integration checks using a real, short WebM video; no account needed."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import threading
import time
import unittest

from playwright.sync_api import sync_playwright

from watch import Watcher, click_next, find_video, video_state


HERE = Path(__file__).resolve().parent


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


class PlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(HERE)))
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("WATCH_BROWSER_PATH") or None,
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.base + "/fixture.html")
        self.page.frame_locator("iframe").locator("video").wait_for()

    def tearDown(self):
        self.page.close()

    def until(self, callback, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if callback():
                return
            self.page.wait_for_timeout(50)
        self.fail("Timed out waiting for expected browser behavior")

    def test_real_playback_hover_and_next_inside_iframe(self):
        watcher = Watcher(self.page)

        def step():
            watcher.tick()
            return self.page.frames[1].evaluate("window.lesson === 2")

        self.until(step)
        self.assertEqual(watcher.completed, 1)
        watcher.tick()
        video = find_video(self.page)
        self.until(lambda: video_state(video)["time"] > 0)
        self.assertFalse(video_state(video)["paused"])
        self.assertTrue(video.evaluate("v => v.matches(':hover')"))
        frame = self.page.frames[1]
        self.assertFalse(frame.locator("input[type=radio]").is_checked())
        self.assertEqual(frame.evaluate("window.quizSubmissions"), 0)

    def test_explicit_selector_for_nonsemantic_control(self):
        frame = self.page.frames[1]
        frame.evaluate("document.querySelector('#next').outerHTML = "
                       "'<div class=next onclick=nextLesson()>→</div>'")
        self.assertFalse(click_next(self.page))
        self.assertTrue(click_next(self.page, ".next"))
        self.assertEqual(frame.evaluate("window.lesson"), 2)

    def test_missing_next_waits_without_replaying(self):
        frame = self.page.frames[1]
        frame.locator("#next").evaluate("b => b.remove()")
        watcher = Watcher(self.page)

        def step():
            watcher.tick()
            return watcher.manual

        self.until(step)
        self.assertEqual(watcher.completed, 1)
        for _ in range(3):
            watcher.tick()
        self.assertEqual(watcher.completed, 1)
        self.assertTrue(video_state(find_video(self.page))["ended"])

    def test_text_navigation_without_button_role(self):
        frame = self.page.frames[1]
        frame.evaluate("document.querySelector('#next').outerHTML = "
                       "'<div onclick=nextLesson()>下一节</div>'")
        self.assertTrue(click_next(self.page))
        self.assertEqual(frame.evaluate("window.lesson"), 2)

    def test_failed_transition_is_not_clicked_repeatedly(self):
        frame = self.page.frames[1]
        frame.evaluate("() => { window.clicks = 0; "
                       "document.querySelector('#next').onclick = () => window.clicks++; }")
        watcher = Watcher(self.page)

        def step():
            watcher.tick()
            return watcher.pending is not None

        self.until(step)
        watcher.pending_since -= 46
        watcher.tick()
        watcher.tick()
        self.assertIsNone(watcher.pending)
        self.assertTrue(watcher.manual)
        self.assertEqual(frame.evaluate("window.clicks"), 1)

    def test_no_video_and_disabled_navigation(self):
        self.page.frames[1].locator("video").evaluate("v => v.remove()")
        self.page.frames[1].locator("#next").evaluate("b => b.disabled = true")
        self.assertIsNone(find_video(self.page))
        self.assertFalse(click_next(self.page))
        Watcher(self.page).tick()


if __name__ == "__main__":
    unittest.main()
