"""Browser integration checks using a real, short WebM video; no account needed."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import threading
import time
import unittest

from playwright.sync_api import Error, sync_playwright

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
        last_error = None
        while time.monotonic() < deadline:
            try:
                if callback():
                    return
            except Error as exc:
                # Match the app loop: frames can detach mid-tick during navigation.
                last_error = exc
            self.page.wait_for_timeout(50)
        self.fail(f"Timed out waiting for expected browser behavior: {last_error}")

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

    def test_video_then_quiz_then_video(self):
        self.page.frames[1].evaluate("() => { document.querySelector('#next').onclick = "
                                    "() => location.href = 'quiz.html'; }")
        watcher = Watcher(self.page)

        def step():
            watcher.tick()
            frame = self.page.frames[1]
            return (frame.url.endswith('player.html')
                    and frame.evaluate("sessionStorage.getItem('quizSubmissions')") == '0')

        self.until(step)
        self.assertEqual(watcher.completed, 1)
        frame = self.page.frames[1]
        self.assertEqual(frame.evaluate("sessionStorage.getItem('quizAnswered')"), 'false')
        watcher.tick()
        video = find_video(self.page)
        self.until(lambda: video_state(video)["time"] > 0)
        self.assertFalse(video_state(video)["paused"])

    def test_quiz_transition_not_repeated_and_loading_video_not_skipped(self):
        frame = self.page.frames[1]
        frame.goto(self.base + '/quiz.html')
        frame.evaluate("() => { document.querySelector('#next').onclick = () => window.nextClicks++; }")
        watcher = Watcher(self.page)
        watcher.tick()
        self.assertEqual(frame.evaluate('window.nextClicks'), 0)
        watcher.quiz_since -= 4
        watcher.tick()
        watcher.tick()
        self.assertEqual(frame.evaluate('window.nextClicks'), 1)
        self.assertFalse(frame.locator('input').is_checked())

        # A not-yet-visible video means the lesson is still loading, not quiz-only.
        frame.evaluate("() => { const v = document.createElement('video'); "
                       "v.style.display = 'none'; document.body.append(v); }")
        watcher.skipped_quizzes.clear()
        watcher.tick()
        self.assertEqual(frame.evaluate('window.nextClicks'), 1)

    def test_sidebar_quiz_label_does_not_skip_loading_frame(self):
        self.page.frames[1].goto('about:blank')
        self.page.evaluate("""() => {
            window.nextClicks = 0;
            const heading = document.createElement('h1');
            heading.textContent = '章节测验';
            document.body.append(heading);
            const button = document.createElement('button');
            button.textContent = '下一节';
            button.onclick = () => window.nextClicks++;
            document.body.append(button);
        }""")
        watcher = Watcher(self.page)
        watcher.tick()
        watcher.quiz_since -= 4
        watcher.tick()
        self.assertEqual(self.page.evaluate('window.nextClicks'), 0)

    def test_play_button_after_video_and_quiz_loads_uninitialized_video(self):
        frame = self.page.frames[1]
        frame.evaluate("""() => {
            sessionStorage.setItem('nextPlayer', 'gated.html');
            document.querySelector('#next').onclick = () => location.href = 'quiz.html';
        }""")
        watcher = Watcher(self.page)

        def step():
            watcher.tick()
            video = find_video(self.page)
            return (self.page.frames[1].url.endswith('gated.html') and video is not None
                    and video_state(video)['time'] > 0 and not video_state(video)['paused'])

        self.until(step)
        frame = self.page.frames[1]
        self.assertEqual(frame.evaluate('window.playClicks'), 1)
        self.assertEqual(frame.evaluate("sessionStorage.getItem('quizAnswered')"), 'false')
        self.assertEqual(frame.evaluate("sessionStorage.getItem('quizSubmissions')"), '0')

    def test_hidden_video_starts_by_clicking_poster(self):
        frame = self.page.frames[1]
        frame.goto(self.base + '/gated.html?hidden')
        video = find_video(self.page)
        self.assertIsNotNone(video)
        self.assertEqual(video_state(video)['ready'], 0)
        watcher = Watcher(self.page)

        def step():
            watcher.tick()
            state = video_state(video)
            return state['time'] > 0 and not state['paused']

        self.until(step)
        self.assertEqual(frame.evaluate('window.playClicks'), 1)

    def test_explicit_play_selector_for_custom_icon(self):
        frame = self.page.frames[1]
        frame.goto(self.base + '/gated.html')
        frame.locator('button').evaluate("b => { b.className = 'custom-start'; b.removeAttribute('aria-label'); }")
        watcher = Watcher(self.page, play_selector='.custom-start')

        def step():
            watcher.tick()
            return video_state(find_video(self.page))['time'] > 0

        self.until(step)
        self.assertEqual(frame.evaluate('window.playClicks'), 1)

    def test_video_quiz_notice_then_next_video_play(self):
        frame = self.page.frames[1]
        frame.evaluate("""() => {
            sessionStorage.setItem('nextPlayer', 'notice.html');
            document.querySelector('#next').onclick = () => location.href = 'quiz.html';
        }""")
        watcher = Watcher(self.page)

        def step():
            watcher.tick()
            video = find_video(self.page)
            return (self.page.frames[1].url.endswith('gated.html') and video is not None
                    and video_state(video)['time'] > 0 and not video_state(video)['paused'])

        self.until(step)
        frame = self.page.frames[1]
        self.assertEqual(frame.evaluate("sessionStorage.getItem('noticeNextClicks')"), '1')
        self.assertEqual(frame.evaluate("sessionStorage.getItem('noticeStudyClicks')"), '0')
        self.assertEqual(frame.evaluate("sessionStorage.getItem('noticeBackgroundClicks')"), '0')
        self.assertEqual(frame.evaluate("sessionStorage.getItem('quizSubmissions')"), '0')
        self.assertEqual(frame.evaluate('window.playClicks'), 1)

    def test_confirmation_in_main_page_clicked_once_until_hidden(self):
        self.page.goto(self.base + '/notice.html')
        self.page.evaluate("() => { document.querySelector('#confirm-next').onclick = () => window.confirmClicks++; }")
        watcher = Watcher(self.page, next_selector='#background-next')
        for _ in range(3):
            watcher.tick()
        self.assertEqual(self.page.evaluate('window.confirmClicks'), 1)
        self.assertEqual(self.page.evaluate('window.studyClicks'), 0)
        self.assertEqual(self.page.evaluate('window.backgroundClicks'), 0)
        self.page.locator('#overlay').evaluate("e => e.style.display = 'none'")
        watcher.tick()
        self.page.locator('#overlay').evaluate("e => e.style.display = ''")
        watcher.tick()
        self.assertEqual(self.page.evaluate('window.confirmClicks'), 2)

    def test_unrelated_notification_is_not_confirmed(self):
        self.page.goto(self.base + '/notice.html')
        self.page.locator('#notice p').evaluate("e => e.textContent = '确定要退出登录吗？'")
        watcher = Watcher(self.page)
        watcher.tick()
        self.assertEqual(self.page.evaluate('window.confirmClicks'), 0)
        self.assertEqual(self.page.evaluate('window.studyClicks'), 0)


if __name__ == "__main__":
    unittest.main()
