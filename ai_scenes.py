#!/usr/bin/env python3
"""
AI-style lighting scenes for SyncLight.

- Template moods (cinema, focus, aurora…)
- Circadian (time-of-day)
- Activity modes: work / music / video / games
- Auto: classifies frontmost app + screen mood and adapts
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# UI id → scene
SceneRGB = Tuple[int, int, int]


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    blurb: str
    kind: str  # template | circadian | activity | auto
    color: Optional[SceneRGB] = None
    brightness: int = 180
    # usb_mode: static | effect | sound | ambi
    usb_mode: str = "static"
    effect_index: int = 0
    sound_index: int = 0
    room_effect: Optional[int] = None  # BT LEDnet effect id index 0..6
    mirror: bool = False
    accent_floor: Optional[SceneRGB] = None  # optional different floor color


TEMPLATES: List[Scene] = [
    Scene("cinema", "Кино", "Тёплый тусклый зал", "template", (255, 92, 48), 90, "static", mirror=True, accent_floor=(40, 10, 8)),
    Scene("focus", "Фокус", "Холодный нейтральный свет", "template", (190, 210, 255), 160, "static"),
    Scene("chill", "Чилл", "Мягкий закат", "template", (255, 150, 95), 120, "static", accent_floor=(120, 60, 160)),
    Scene("aurora", "Аврора", "Северное сияние", "template", (60, 220, 180), 140, "effect", effect_index=0, room_effect=0),
    Scene("neon", "Неон", "Киберпанк", "template", (255, 40, 180), 200, "effect", effect_index=2, room_effect=5, accent_floor=(40, 200, 255)),
    Scene("reading", "Чтение", "Тёплый бумажный", "template", (255, 210, 150), 110, "static"),
    Scene("party", "Вечеринка", "Динамика и цвет", "template", (255, 80, 200), 230, "effect", effect_index=6, room_effect=1),
    Scene("deep", "Ночь", "Глубокий синий", "template", (20, 40, 120), 55, "static", accent_floor=(10, 0, 30)),
]

ACTIVITIES: List[Scene] = [
    Scene("work", "Работа", "Спокойный холодный свет", "activity", (200, 215, 255), 140, "static"),
    Scene("music", "Музыка", "Реакция на звук + комната", "activity", (180, 70, 255), 200, "sound", sound_index=0, room_effect=5, mirror=False),
    Scene("video", "Видео", "Fullscreen → цвета кадра на ленту и в комнату", "activity", (255, 180, 100), 200, "ambi", mirror=True),
    Scene("games", "Игры", "Яркая динамика", "activity", (40, 255, 120), 230, "effect", effect_index=3, room_effect=2, mirror=True),
]

META: List[Scene] = [
    Scene("circadian", "Сутки", "Свет по времени дня", "circadian", brightness=160, usb_mode="static"),
    Scene("auto", "Авто ИИ", "Смотрит, чем занят · подстраивает свет", "auto", brightness=180, usb_mode="static", mirror=True),
]


def all_scenes() -> List[Scene]:
    return [*META, *ACTIVITIES, *TEMPLATES]


def scenes_payload() -> List[dict]:
    out = []
    for s in all_scenes():
        out.append(
            {
                "id": s.id,
                "title": s.title,
                "blurb": s.blurb,
                "kind": s.kind,
                "color": list(s.color) if s.color else None,
                "brightness": s.brightness,
            }
        )
    return out


def get_scene(scene_id: str) -> Optional[Scene]:
    for s in all_scenes():
        if s.id == scene_id:
            return s
    return None


def circadian_color(now: Optional[dt.datetime] = None) -> Tuple[SceneRGB, int, str]:
    """Return (rgb, brightness, phase label) for local time."""
    now = now or dt.datetime.now()
    h = now.hour + now.minute / 60.0
    if 5 <= h < 8:
        return (255, 190, 140), 130, "рассвет"
    if 8 <= h < 11:
        return (220, 230, 255), 170, "утро"
    if 11 <= h < 16:
        return (240, 245, 255), 190, "день"
    if 16 <= h < 19:
        return (255, 200, 140), 160, "вечер"
    if 19 <= h < 22:
        return (255, 120, 70), 110, "закат"
    if 22 <= h < 24 or 0 <= h < 5:
        return (40, 30, 90), 45, "ночь"
    return (200, 200, 220), 150, "день"


# Bundle / name hints → activity
_WORK = (
    "code", "cursor", "xcode", "terminal", "iterm", "notion", "obsidian", "word",
    "excel", "pages", "numbers", "keynote", "slack", "telegram", "mail",
    "figma", "sketch", "photoshop", "preview",
    "finder", "zoom", "teams", "webstorm", "pycharm", "vscode", "sublime", "bear",
    "craft", "notes", "calendar", "reminders",
)
# Browsers classified as work unless fullscreen video (handled in resolve_auto)
_MUSIC = (
    "spotify", "music", "itunes", "voicememos", "djay", "ableton", "logic",
    "garageband", "youtube music", "soundcloud", "foobar", "vlc",
)
_VIDEO = (
    "tv", "netflix", "youtube", "iina", "quicktime", "plex", "kino", "mpv",
    "disney", "prime", "ivi", "kinopoisk", "photos",
)
_GAMES = (
    "steam", "dota", "cs2", "counter-strike", "minecraft", "epic", "geforce",
    "game", "unity", "unreal", "playstation", "xbox", "openemu", "porting kit",
    "whisky", "crossover", "battle.net", "riot", "league", "valorant",
)


def classify_activity(app_name: str = "", bundle_id: str = "") -> str:
    blob = f"{app_name} {bundle_id}".lower()
    for key in _GAMES:
        if key in blob:
            return "games"
    for key in _MUSIC:
        if key in blob:
            return "music"
    for key in _VIDEO:
        if key in blob:
            return "video"
    for key in _WORK:
        if key in blob:
            return "work"
    # Default: work-ish calm
    return "work"


def mood_from_screen(avg: SceneRGB, motion: float = 0.0) -> str:
    """Rough screen-mood hint from average RGB + motion 0..1."""
    r, g, b = avg
    lum = (r + g + b) / 3.0
    sat = max(r, g, b) - min(r, g, b)
    if motion > 0.35 and sat > 40:
        return "games"
    if lum < 45:
        return "video"
    if sat < 25 and lum > 120:
        return "work"
    if r > g + 30 and r > b + 20:
        return "cinema_warm"
    return "work"


def blend(a: SceneRGB, b: SceneRGB, t: float) -> SceneRGB:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def resolve_auto(
    app_name: str,
    bundle_id: str,
    screen_avg: Optional[SceneRGB] = None,
    motion: float = 0.0,
    *,
    fullscreen: bool = False,
    cinematic: bool = False,
    video_app: bool = False,
) -> Tuple[str, Scene]:
    """
    Pick activity scene. Screen mood + fullscreen video strongly prefer Ambilight.
    Returns (activity_id, Scene).
    """
    act = classify_activity(app_name, bundle_id)
    # Theater mode: fullscreen player / cinematic frame → follow screen colours
    if cinematic or (fullscreen and (video_app or act == "video")):
        act = "video"
    elif screen_avg is not None:
        mood = mood_from_screen(screen_avg, motion)
        if act == "work" and mood in ("games", "video"):
            act = mood if mood != "cinema_warm" else "video"
        if mood == "cinema_warm" and act == "work":
            # stay work but warmer — handled by caller tint
            pass
        # Dark fullscreen browser (YouTube FS) without bundle hint
        if fullscreen and mood in ("video", "cinema_warm"):
            act = "video"
    scene = get_scene(act) or ACTIVITIES[0]
    return act, scene


def scene_by_id_map() -> Dict[str, Scene]:
    return {s.id: s for s in all_scenes()}
