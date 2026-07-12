"""Model shapes for the storylines redesign."""

from journal.models import Storyline, StorylineChapter


def test_chapter_defaults_are_draft_shaped() -> None:
    ch = StorylineChapter(id=1, storyline_id=1, seq=1)
    assert ch.state == "draft"
    assert ch.segments == []
    assert ch.addenda == []
    assert ch.published_at is None and ch.read_at is None


def test_storyline_has_no_window_fields() -> None:
    s = Storyline(id=1, user_id=1, name="Running")
    assert not hasattr(s, "start_date")
    assert not hasattr(s, "summary_embedding")


def test_panel_model_is_gone() -> None:
    import journal.models as m
    assert not hasattr(m, "StorylinePanel")
