"""Document the display contract for dwell / grade (mirrors frontend format.ts)."""


def format_dwell(minutes):
    if minutes is None or minutes < 0:
        return "—"
    m = round(minutes)
    if m < 8:
        return "刚进"
    if m < 60:
        return f"{m}m"
    h, rem = divmod(m, 60)
    return f"{h}h{rem}m" if rem else f"{h}h"


def format_grade(grade):
    if not grade or grade in ("UNCLASSIFIED", "INSUFFICIENT"):
        return "未分级"
    return grade


def test_dwell_just_entered_not_raw_zero():
    assert format_dwell(0) == "刚进"
    assert format_dwell(0.01) == "刚进"
    assert format_dwell(30.006) == "30m"
    assert format_dwell(240.010906) == "4h"
    assert format_dwell(44.999) == "45m"


def test_grade_unclassified_not_clipped_english():
    assert format_grade("UNCLASSIFIED") == "未分级"
    assert format_grade("A") == "A"
    assert format_grade("D") == "D"


if __name__ == "__main__":
    test_dwell_just_entered_not_raw_zero()
    test_grade_unclassified_not_clipped_english()
    print("ok")
