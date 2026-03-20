from __future__ import annotations

from datetime import datetime

from models.video import Video


def calc_trend_score(
    views: int,
    likes: int,
    comments: int,
    shares: int,
    hours: float,
) -> float:
    target_velocity = 5000.0
    target_e = 0.1
    target_s = 0.01
    target_c = 0.02

    safe_hours = max(hours, 1.0)
    safe_views = max(views, 1)

    velocity_score = min(1.0, (views / safe_hours) / target_velocity)
    engagement_score = min(1.0, (likes + 2 * comments + 3 * shares) / (safe_views * target_e))
    share_score = min(1.0, (shares / safe_views) / target_s)
    comment_score = min(1.0, (comments / safe_views) / target_c)

    return 0.4 * velocity_score + 0.25 * engagement_score + 0.2 * share_score + 0.15 * comment_score


def get_video_trend_score(video: Video, now: datetime | None = None) -> float | None:
    if not video.upload_time or video.view_count is None:
        return None
    current_time = now or datetime.now()
    hours = max((current_time - video.upload_time).total_seconds() / 3600.0, 1.0)
    return calc_trend_score(
        views=max(video.view_count or 0, 0),
        likes=max(video.like_count or 0, 0),
        comments=max(video.comment_count or 0, 0),
        shares=max(video.share_count or 0, 0),
        hours=hours,
    )


def get_trend_level(score: float) -> str:
    if score > 0.9:
        return "Viral"
    if score > 0.8:
        return "Trending"
    if score >= 0.6:
        return "Potential"
    if score >= 0.4:
        return "Normal"
    return "Cold"
