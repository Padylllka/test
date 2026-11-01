"""Visible browser playback assistant. Run `python watch.py --help`."""

import argparse
import logging
from pathlib import Path
import re
import time

from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout, sync_playwright


LOG = logging.getLogger("watch")
NEXT_NAME = re.compile(
    r"^\s*(?:下一节|下一集|下一章|下一个视频|下一视频|Next video|Next lesson|Следующее видео)\s*$",
    re.IGNORECASE,
)
QUIZ_NAME = re.compile(r"章节测验|章节测试|课后测验|随堂测验|本章测试|单元测验|章节作业|随堂测试")
PLAY_NAME = re.compile(r"^\s*(?:播放|播放视频|开始播放|Play|Play video|Start video|Воспроизвести)\s*$", re.I)
ROOT = Path(__file__).resolve().parent


def video_state(video):
    return video.evaluate("""v => {
        window.__watchVideoIds ??= new WeakMap();
        if (!window.__watchVideoIds.has(v))
            window.__watchVideoIds.set(v, crypto.randomUUID());
        return {
            id: window.__watchVideoIds.get(v), src: v.currentSrc,
            ended: v.ended, paused: v.paused, time: v.currentTime,
            ready: v.readyState, error: !!v.error
        };
    }""")


def find_video(page, selector="video", play_selector=None):
    """Include nested and cross-origin frames; ignore hidden preload players."""
    hidden = []
    for frame in page.frames:
        try:
            for video in frame.locator(selector).all():
                if video.is_visible():
                    return video
                hidden.append(video)
        except Error:
            # Navigation may detach a frame while enumerating players.
            continue
    # Some players hide the media element until their poster's Play is clicked.
    for video in hidden:
        try:
            if find_play_control(video, play_selector) is not None:
                return video
        except Error:
            continue
    return None


def find_play_control(video, selector=None):
    if selector:
        frame = video.element_handle(timeout=1000).owner_frame()
        candidates = frame.locator(selector).all() if frame else []
    else:
        # Keep automatic clicks within this player's wrapper, away from other videos.
        wrapper = video.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' video-js ')][1]")
        if not wrapper.count():
            wrapper = video.locator("xpath=..")
        candidates = (wrapper.locator(".vjs-big-play-button, .vjs-play-control").all()
                      + wrapper.get_by_role("button", name=PLAY_NAME).all())
    for control in candidates:
        if control.is_visible() and control.is_enabled():
            return control
    return None


def point_at_video(page, video):
    # A poster overlay may intercept hover()/click() on the video itself.
    if not video.is_visible():
        return
    video.scroll_into_view_if_needed(timeout=2000)
    box = video.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def hover_and_play(page, video, state, play_selector=None):
    point_at_video(page, video)
    if state["paused"] and not state["ended"] and not state["error"]:
        control = find_play_control(video, play_selector)
        if control is not None:
            control.click(timeout=2000)
            LOG.info("Нажал кнопку воспроизведения. Проверяю запуск видео.")
        elif state["ready"] >= 1 and not play_selector:
            # Native HTML5 fallback when the player has no separate Play button.
            video.click(timeout=2000)
            error = video.evaluate("async v => { try { await v.play(); return null; } catch(e) { return e.name; } }")
            if error:
                LOG.warning("Браузер не запустил видео (%s). Попробую снова.", error)
        point_at_video(page, video)


def click_next(page, selector=None):
    """Click an explicit selector or a narrowly named lesson-navigation control."""
    for frame in page.frames:
        candidates = []
        try:
            if selector:
                candidates = frame.locator(selector).all()
            else:
                candidates = (
                    frame.get_by_role("button", name=NEXT_NAME).all()
                    + frame.get_by_role("link", name=NEXT_NAME).all()
                    + frame.get_by_text(NEXT_NAME).all()
                )
            for button in candidates:
                if button.is_visible() and button.is_enabled():
                    button.click(timeout=3000)
                    return True
        except Error:
            continue
    return False


def quiz_page_key(page):
    """Recognize a quiz-only view, while allowing a loading video to appear."""
    for frame in page.frames:
        try:
            for video in frame.locator("video").all():
                if not video_state(video)["ended"]:
                    return None
        except Error:
            return None
    for frame in page.frames:
        try:
            if frame.parent_frame and not frame.frame_element().is_visible():
                continue
            text = frame.locator("body").inner_text(timeout=1000)
            quiz_url = re.search(r"/(?:work|work-ans)/", frame.url)
            # A parent course sidebar may mention quizzes while its player loads.
            if frame.child_frames and not quiz_url:
                continue
            questions = frame.locator('input[type="radio"]:visible, input[type="checkbox"]:visible').count()
            if QUIZ_NAME.search(text) or quiz_url or ("测验" in text and questions):
                document_id = frame.evaluate("""() => {
                    window.__watchQuizDocumentId ??= crypto.randomUUID();
                    return window.__watchQuizDocumentId;
                }""")
                return (frame.url, document_id)
        except Error:
            continue
    return None


class Watcher:
    def __init__(self, page, next_selector=None, video_selector="video", transition_timeout=45, play_selector=None):
        self.page = page
        self.next_selector = next_selector
        self.video_selector = video_selector
        self.transition_timeout = transition_timeout
        self.play_selector = play_selector
        self.started = None
        self.current = None
        self.play_attempt = 0.0
        self.pending = None
        self.pending_since = 0.0
        self.manual = False
        self.completed = 0
        self.progress = None
        self.progress_since = time.monotonic()
        self.notice_at = 0.0
        self.quiz_candidate = None
        self.quiz_since = 0.0
        self.skipped_quizzes = set()

    def skip_quiz(self):
        key = quiz_page_key(self.page)
        if key is None:
            self.quiz_candidate = None
            return False
        now = time.monotonic()
        if key != self.quiz_candidate:
            self.quiz_candidate = key
            self.quiz_since = now
        # Do not click during transient navigation or repeatedly on a blocked quiz.
        if now - self.quiz_since < 3 or key in self.skipped_quizzes:
            return True
        if click_next(self.page, self.next_selector):
            self.skipped_quizzes.add(key)
            self.pending = None
            LOG.info("Открыт тест без видео. Нажал 下一节, ответы оставлены пустыми.")
        else:
            self.notice("Открыт тест, но 下一节 недоступна. Жду доступной кнопки или ручного перехода.")
        return True

    def notice(self, message):
        now = time.monotonic()
        if now - self.notice_at >= 30:
            LOG.info(message)
            self.notice_at = now

    def tick(self):
        video = find_video(self.page, self.video_selector, self.play_selector)
        if video is None:
            if self.skip_quiz():
                return
            self.notice("Видео не найдено. Войди в аккаунт и открой нужный урок в этом окне.")
            return
        self.quiz_candidate = None
        state = video_state(video)
        key = (state["id"], state["src"])
        now = time.monotonic()
        if key != self.current:
            self.current = key
            self.play_attempt = 0.0
            self.manual = False
            self.started = None
            LOG.info("Найден плеер. Ожидаю загрузку и запускаю воспроизведение.")

        # A next button may asynchronously replace the page/frame or reuse a video.
        if self.pending is not None:
            if key != self.pending or (not state["ended"] and state["time"] < 2):
                self.pending = None
                self.manual = False
                LOG.info("Открылось следующее видео.")
            elif now - self.pending_since >= self.transition_timeout:
                self.pending = None
                self.manual = True
                LOG.warning("После нажатия видео не сменилось. Открой следующий урок вручную.")
            else:
                point_at_video(self.page, video)
                return

        if state["ended"]:
            point_at_video(self.page, video)
            if not self.manual:
                self.completed += 1
                LOG.info("Видео завершено. Завершённых видео за запуск: %d.", self.completed)
                # Mark first so a timeout cannot cause repeated navigation clicks.
                self.manual = True
                if click_next(self.page, self.next_selector):
                    self.pending = key
                    self.pending_since = now
                else:
                    LOG.warning("Кнопка следующего урока не найдена. Открой урок вручную "
                                "или перезапусти с --next-selector. Возможно, курс завершён.")
            return

        # Also resume after a manual seek/replay on the same element.
        self.manual = False
        if state["error"]:
            self.notice("Плеер сообщил об ошибке загрузки видео. Проверь страницу вручную.")
            return
        # Play may be the action which loads metadata, so don't wait for readyState.
        if state["paused"] and now - self.play_attempt >= 10:
            self.play_attempt = now
            hover_and_play(self.page, video, state, self.play_selector)
        else:
            point_at_video(self.page, video)
        if not state["paused"] and state["time"] > 0 and self.started != key:
            self.started = key
            LOG.info("Видео воспроизводится.")
        progress = (key, state["time"])
        if progress != self.progress:
            self.progress = progress
            self.progress_since = now
        elif now - self.progress_since > 60:
            self.notice("Воспроизведение не продвигается больше минуты. "
                        "Проверь буферизацию, окно задания или сообщение сайта.")


def main():
    parser = argparse.ArgumentParser(description="Воспроизведение и переключение видео 超星 в видимом браузере.")
    parser.add_argument("--url", default="https://mooc2-ans.chaoxing.com/", help="Адрес страницы курса")
    parser.add_argument("--next-selector", help="Playwright/CSS-селектор кнопки следующего урока")
    parser.add_argument("--video-selector", default="video", help="Селектор HTML5-плеера")
    parser.add_argument("--play-selector", help="Селектор кнопки воспроизведения внутри фрейма плеера")
    parser.add_argument("--profile", type=Path, default=ROOT / ".local" / "browser-profile")
    parser.add_argument("--channel", choices=["chrome", "msedge"], help="Использовать установленный Chrome/Edge")
    parser.add_argument("--poll", type=float, default=1.0, help="Интервал проверки, секунды (минимум 0.2)")
    args = parser.parse_args()
    if args.poll < 0.2:
        parser.error("--poll должен быть не меньше 0.2")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    args.profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(args.profile.resolve()), headless=False, channel=args.channel,
            no_viewport=True,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeout:
                LOG.warning("Страница загружается долго; продолжаю наблюдение за плеером.")
            LOG.info("Войди в аккаунт и открой первое видео в этой вкладке. "
                     "Оставь окно видимым. Остановка: Ctrl+C или закрытие окна браузера.")
            watcher = Watcher(page, args.next_selector, args.video_selector, play_selector=args.play_selector)
            while not page.is_closed():
                try:
                    watcher.tick()
                except Error:
                    if page.is_closed():
                        break
                    watcher.notice("Страница или плеер меняется. Повторю проверку.")
                page.wait_for_timeout(args.poll * 1000)
        except KeyboardInterrupt:
            LOG.info("Остановлено пользователем.")
        except Error as exc:
            if context.pages:
                # Avoid dumping page URLs, which may contain course access tokens.
                LOG.error("Ошибка браузера (%s). Перезапусти скрипт и проверь страницу.", type(exc).__name__)
        finally:
            try:
                context.close()
            except Error:
                pass


if __name__ == "__main__":
    main()
