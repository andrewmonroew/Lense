import sys
import os
import time

# Add root folder to path to allow importing src.* package modules
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from PySide6.QtWidgets import QApplication, QSplashScreen
from src.ui.main_window import MainWindow
from src.core.crash_log import install_crash_logger
from src.core.utils import get_data_dir
from src.core.version import APP_NAME, APP_VERSION

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QIcon, QPixmap

# How long the splash stays up at minimum. MainWindow construction alone is fast
# enough that the splash would otherwise be created and finished within the same
# handful of milliseconds -- never actually painted by the window manager, which
# reads as "no splash at all".
SPLASH_MIN_SECONDS = 2.2
SPLASH_SIZE = 640  # rendered size of the logo on the splash, in pixels

# The splash chime. When it loads, the splash is held for its full length instead of
# SPLASH_MIN_SECONDS -- cutting the sound off partway through sounds like a crash, not
# like a startup. Capped so a long file can never turn launching the app into a wait.
SPLASH_SOUND = "Weird2.wav"
SPLASH_MAX_SECONDS = 6.0
SPLASH_SOUND_TAIL = 0.2  # a beat of silence after the last sample, before the handover


def _wav_duration(path):
    """Length of a .wav in seconds, or 0.0 if it can't be read."""
    import contextlib
    import wave
    try:
        with contextlib.closing(wave.open(path, "rb")) as handle:
            rate = handle.getframerate()
            return handle.getnframes() / float(rate) if rate else 0.0
    except Exception:
        return 0.0


def _log_splash_sound(message):
    """One line in logs/splash_sound.log saying how the chime was played.

    Audio fails for reasons the app cannot see -- no backend, output routed to a device
    nobody is listening to, a per-application stream muted by the mixer. Silently
    swallowing that leaves "it didn't play" with nothing to go on, so record which path
    was taken. Never raises: this is diagnostics, not a feature.
    """
    try:
        from src.core.utils import get_logs_dir
        import datetime
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(os.path.join(get_logs_dir(), "splash_sound.log"), "a") as handle:
            handle.write(f"{stamp}  {message}\n")
    except Exception:
        pass


def _play_via_system_player(path):
    """Hand the file to whatever audio player the OS ships. True if one was started.

    The fallback for when Qt reports success but nothing comes out. These players pick
    their own output device and respect the user's current default, which is the usual
    difference between silence and sound.
    """
    import shutil
    import subprocess

    if sys.platform.startswith("win"):
        try:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            _log_splash_sound("played via winsound")
            return True
        except Exception as exc:
            _log_splash_sound(f"winsound failed: {exc!r}")
            return False

    if sys.platform == "darwin":
        candidates = [["afplay", path]]
    else:
        candidates = [["paplay", path],
                      ["pw-play", path],
                      ["aplay", "-q", path],
                      ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]]
    for command in candidates:
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log_splash_sound(f"played via {command[0]}")
            return True
        except Exception as exc:
            _log_splash_sound(f"{command[0]} failed: {exc!r}")
    _log_splash_sound("no system audio player available")
    return False


def _load_splash_sound(path):
    """(player, audio_output) for the splash chime, or None. Both must be kept alive.

    QMediaPlayer rather than the more obvious QSoundEffect, on purpose. QSoundEffect
    tags its stream with media.role="event" -- the sound server's *system event sounds*
    category, the one notification beeps go through. On any desktop where those are
    turned off (a very common setting) the server mutes the stream before it reaches the
    speakers, while Qt still reports it loaded, playing, and unmuted: perfectly silent
    with nothing anywhere saying why. QMediaPlayer opens an ordinary playback stream
    that follows the normal output volume instead.

    Everything here is best-effort: a machine with no audio device, no QtMultimedia
    backend, or no sound file still has to launch normally. A startup jingle is never
    worth failing to start over.
    """
    if not os.path.exists(path):
        _log_splash_sound(f"missing sound file: {path}")
        return None
    try:
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        audio_output = QAudioOutput()
        audio_output.setVolume(0.9)
        player = QMediaPlayer()
        player.setAudioOutput(audio_output)
        player.setSource(QUrl.fromLocalFile(path))
        # The QAudioOutput is returned alongside the player because QMediaPlayer does
        # not take ownership of it -- drop the reference and playback goes silent.
        return player, audio_output
    except Exception as exc:
        _log_splash_sound(f"QMediaPlayer unavailable: {exc!r}")
        return None


def _play_splash_sound(app, path, handle):
    """Starts the chime, falling back to an OS player if Qt can't.

    QMediaPlayer loads its source asynchronously, so it gets waited on -- briefly, and
    never past the splash's own minimum, so a stalled load costs no visible startup
    time. If it still won't run, hand the file to a system player rather than starting
    up mute.
    """
    if handle is None:
        return _play_via_system_player(path)

    from PySide6.QtMultimedia import QMediaPlayer
    player, _audio_output = handle
    for _ in range(int(SPLASH_MIN_SECONDS * 100)):
        status = player.mediaStatus()
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia,
                      QMediaPlayer.InvalidMedia):
            break
        app.processEvents()
        time.sleep(0.01)

    if player.error() != QMediaPlayer.NoError:
        _log_splash_sound(f"QMediaPlayer error: {player.errorString()}")
        return _play_via_system_player(path)

    player.play()
    # Playback runs on the audio backend's own thread, so it carries on through the
    # MainWindow construction that follows without needing the event loop. Give it a
    # pump to actually start before deciding whether it did.
    app.processEvents()
    if player.playbackState() != QMediaPlayer.PlayingState:
        _log_splash_sound(f"QMediaPlayer did not start (state={player.playbackState()})")
        return _play_via_system_player(path)

    _log_splash_sound("played via QMediaPlayer")
    return True

def _build_splash_pixmap(logo_pixmap, size=SPLASH_SIZE):
    """Scales the logo for the splash, preserving its alpha channel so the splash reads
    as just the logo floating on the desktop rather than a plate behind it. Paired with
    WA_TranslucentBackground on the QSplashScreen itself (see main) -- without that
    attribute the window's uncovered area paints opaque and the transparent corners come
    out as a solid box."""
    return logo_pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

def main():
    install_crash_logger()

    # Qt's ffmpeg multimedia backend dumps the decoded stream's format to stderr the
    # first time it opens a file ("Input #0, wav, from ..."), which is pure noise on
    # every launch from a terminal. setdefault so anyone debugging with their own
    # QT_LOGGING_RULES still wins.
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Lense")  # QSettings (last-opened-project memory) keys off this

    data_dir = get_data_dir()
    logo_path = os.path.join(data_dir, "logo.png")
    # Prefer the multi-resolution .ico for OS chrome (taskbar, titlebar, alt-tab):
    # it carries 16-256px frames so the window manager picks a crisp one instead of
    # downscaling a single 2048px PNG. The PNG stays the splash artwork.
    icon_path = os.path.join(data_dir, "Catmera.ico")
    if not os.path.exists(icon_path):
        icon_path = logo_path
    app_icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
    app.setWindowIcon(app_icon)  # taskbar/dock icon, and the default for every window

    # Splash screen -- shown immediately (before the heavier MainWindow construction,
    # which loads the whole equipment catalog) and closed once the window is ready.
    splash = None
    splash_shown_at = None
    splash_sound = None
    splash_hold = SPLASH_MIN_SECONDS
    logo_pixmap = QPixmap(logo_path)
    if not logo_pixmap.isNull():
        splash_pixmap = _build_splash_pixmap(logo_pixmap)
        splash = QSplashScreen(splash_pixmap, Qt.WindowStaysOnTopHint)
        # Let the logo's own alpha through instead of painting a plate behind it. No
        # loading caption here on purpose: over a transparent window the text would sit
        # on the bare desktop, where any fixed color is unreadable against some
        # wallpapers -- the logo alone is the cleaner read.
        splash.setAttribute(Qt.WA_TranslucentBackground, True)
        splash.show()
        splash.raise_()
        splash_shown_at = time.monotonic()
        # A single processEvents() isn't reliably enough for the window manager to map
        # AND paint a brand-new window -- pump briefly until it's actually on screen.
        for _ in range(20):
            app.processEvents()
            time.sleep(0.01)

        sound_path = os.path.join(data_dir, SPLASH_SOUND)
        # Read straight from QSettings rather than through the settings module, which
        # would drag the whole UI package into the splash path before the app window
        # is even built.
        from PySide6.QtCore import QSettings
        chime_wanted = QSettings().value("startup/splash_sound", True)
        if isinstance(chime_wanted, str):
            chime_wanted = chime_wanted.strip().lower() in ("true", "1", "yes", "on")
        splash_sound = _load_splash_sound(sound_path) if chime_wanted else None
        if chime_wanted and _play_splash_sound(app, sound_path, splash_sound):
            splash_hold = min(SPLASH_MAX_SECONDS,
                              max(splash_hold,
                                  _wav_duration(sound_path) + SPLASH_SOUND_TAIL))

    # Force Fusion -- it's the one Qt style that's entirely self-painted rather than
    # delegating to the native OS theme engine, so the dark stylesheet below renders
    # identically everywhere. Without this, Windows falls back to "windowsvista" /
    # "windows11", which draws scrollbars, combo boxes, spin arrows, etc. via the
    # actual Windows theme API and mostly ignores QSS colors on them -- native light
    # chrome bleeding through a dark stylesheet, which is what "looks broken" here.
    app.setStyle("Fusion")

    window = MainWindow()
    window.setWindowIcon(app_icon)

    # Hold the splash for its full duration before handing over to the main window,
    # keeping the event loop pumping so it stays painted rather than freezing.
    if splash is not None and splash_shown_at is not None:
        while time.monotonic() - splash_shown_at < splash_hold:
            app.processEvents()
            time.sleep(0.01)

    window.show()

    if splash is not None:
        splash.finish(window)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
