"""readaloud.speech's word timings as they were before their Rust port: the
oracle for tests/test_core_speech.py.

`word_at` is the old `Spoken.word_at`, a linear scan, as a function.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from readaloud.speech import Timed


def word_at(timings: Sequence[Timed], seconds: float) -> int | None:
    found: int | None = None
    for t in timings:
        if t.start > seconds:
            break
        found = t.word_slot
    return found


_MAX_SKIP = 12  # source chars misaki may drop inside one token
_SEARCH_WINDOW = 400  # how far ahead of the cursor a token may start


def _locate(text: str, needle: str, cursor: int) -> tuple[int, int] | None:
    """Find `needle`'s char span in `text` at/after `cursor`, tolerating the ways
    misaki mangles token text (absorbed leading whitespace, dropped quotes)."""
    if not needle:
        return None
    n = len(text)

    # 1. exact match separated from the cursor by whitespace only (the normal case).
    pos = text.find(needle, cursor)
    if pos >= 0 and not text[cursor:pos].strip():
        return pos, pos + len(needle)

    # 2. misaki sometimes glues leading whitespace/newlines onto .text; retry trimmed.
    trimmed = needle.strip()
    if trimmed and trimmed != needle:
        p = text.find(trimmed, cursor)
        if p >= 0 and not text[cursor:p].strip():
            return p, p + len(trimmed)
    else:
        trimmed = needle

    # 3. subsequence match allowing dropped source characters (e.g. print('hello).
    limit = min(n, cursor + _SEARCH_WINDOW)
    first = trimmed[0]
    for start in range(cursor, limit):
        if text[start] != first:
            continue
        i, j, skipped = start, 0, 0
        while i < n and j < len(trimmed):
            if text[i] == trimmed[j]:
                i += 1
                j += 1
            else:
                i += 1
                skipped += 1
                if skipped > _MAX_SKIP:
                    break
        if j == len(trimmed):
            return start, i

    # 4. last resort: a plain forward exact match, even across a non-whitespace gap.
    if pos >= 0:
        return pos, pos + len(needle)
    p = text.find(trimmed, cursor)
    if p >= 0:
        return p, p + len(trimmed)
    return None


def align_words(
    text: str,
    spans: Sequence[tuple[int, int]],
    results: Iterable[tuple[Sequence[Any], float]],
) -> list[tuple[int, float, float]]:
    """Map misaki tokens onto word slots.

    `text`     the exact string handed to the pipeline (chunk.text).
    `spans`    [(start, end)] char offsets of each word slot inside `text`, in
               order, non-overlapping.
    `results`  one (tokens, audio_duration_seconds) pair per pipeline Result, in
               yield order.  Timestamps inside each Result are relative to that
               Result's own audio, so they are offset by the cumulative duration
               of everything yielded before it.

    Returns [(word_slot, start, end)] for every word slot, sorted by slot index,
    with monotonically non-decreasing, non-negative times.
    """
    word_spans_ = spans
    nwords = len(word_spans_)
    if nwords == 0:
        return []

    lo: list[float | None] = [None] * nwords
    hi: list[float | None] = [None] * nwords

    cursor = 0
    time_offset = 0.0
    total = 0.0
    wi = 0  # first word slot that may still overlap (spans is sorted)

    for tokens, duration in results:
        for tok in tokens or ():
            raw = getattr(tok, "text", None) or ""
            if not raw:
                continue
            span = _locate(text, raw, cursor)
            if span is None:
                continue  # unmatchable token: skip, keep the cursor
            s_char, e_char = span
            cursor = e_char

            ts = getattr(tok, "start_ts", None)
            te = getattr(tok, "end_ts", None)
            if ts is None and te is None:
                continue  # e.g. a bare '$' -- consumes text, no timing
            if ts is None:
                ts = te
            if te is None:
                te = ts
            ts = max(0.0, float(ts)) + time_offset
            te = max(0.0, float(te)) + time_offset
            if te < ts:
                ts, te = te, ts

            # advance the word window, then collect every word this token covers
            while wi < nwords and word_spans_[wi][1] <= s_char:
                wi += 1
            covered = []
            k = wi
            while k < nwords and word_spans_[k][0] < e_char:
                a, b = word_spans_[k]
                overlap = min(b, e_char) - max(a, s_char)
                if overlap > 0:
                    covered.append((k, max(a, s_char), min(b, e_char)))
                k += 1
            if not covered:
                continue

            if len(covered) == 1:
                slots = [(covered[0][0], ts, te)]
            else:
                # one token spoke several words ("  spaces\tand"): split its span
                # proportionally to how much of the token each word occupies.
                base = covered[0][1]
                width = max(1, covered[-1][2] - base)
                slots = []
                for idx, a, b in covered:
                    f0 = (a - base) / width
                    f1 = (b - base) / width
                    slots.append((idx, ts + (te - ts) * f0, ts + (te - ts) * f1))

            for idx, a_t, b_t in slots:
                lo[idx] = a_t if lo[idx] is None else min(lo[idx], a_t)
                hi[idx] = b_t if hi[idx] is None else max(hi[idx], b_t)

        time_offset += max(0.0, float(duration))
        total = time_offset

    if total <= 0.0:
        known = [h for h in hi if h is not None]
        total = max(known) if known else 0.0

    # fill untimed slots by interpolating between the timed anchors around them
    i = 0
    prev_end = 0.0
    while i < nwords:
        if lo[i] is not None:
            prev_end = hi[i]  # type: ignore[assignment]
            i += 1
            continue
        j = i
        while j < nwords and lo[j] is None:
            j += 1
        nxt = lo[j] if j < nwords else total
        left, right = prev_end, max(prev_end, nxt)
        widths = [max(1, word_spans_[k][1] - word_spans_[k][0]) for k in range(i, j)]
        tot_w = sum(widths)
        t = left
        for k, w in zip(range(i, j), widths):
            step = (right - left) * (w / tot_w)
            lo[k], hi[k] = t, t + step
            t += step
        prev_end = right
        i = j

    out: list[tuple[int, float, float]] = []
    run = 0.0
    for k in range(nwords):
        a = max(run, float(lo[k]))  # type: ignore[arg-type]
        b = max(a, float(hi[k]))  # type: ignore[arg-type]
        out.append((k, a, b))
        run = a
    return out


# Trailing characters that document.py would not include in Word.text but that
# sit inside a chunk's raw text right after the word.
_TRAILING_PUNCT = "".join(
    (".,;:!?)]}>\"'", "’”…»‘“«*_`~")
)


def word_spans(
    text: str,
    offsets: Sequence[int],
    word_texts: Sequence[str] | None = None,
) -> list[tuple[int, int]]:
    """Derive one (start, end) char span per word slot inside `text`.

    `Chunk.offsets` only carries each word's START offset, and
    `Engine.synth` has no access to the `Document` that owns `Word.text`, so the
    end is inferred: scan to the next whitespace, stop at the next word's offset,
    and trim trailing punctuation that document.py would have excluded.  When the
    caller does have the word texts it can pass them for an exact answer.

    The returned spans are clamped into `text`, non-overlapping and sorted.
    """
    n = len(text)
    spans: list[tuple[int, int]] = []
    cnt = len(offsets)
    floor = 0
    for i in range(cnt):
        try:
            start = int(offsets[i])
        except (TypeError, ValueError):
            start = floor
        start = max(floor, min(n, start))

        if i + 1 < cnt:
            try:
                nxt = int(offsets[i + 1])
            except (TypeError, ValueError):
                nxt = n
            limit = max(start, min(n, nxt))
        else:
            limit = n

        end = -1
        if word_texts is not None and i < len(word_texts):
            wt = word_texts[i] or ""
            if wt and text.startswith(wt, start):
                end = start + len(wt)

        if end < 0:
            j = start
            while j < limit and not text[j].isspace():
                j += 1
            while j > start + 1 and text[j - 1] in _TRAILING_PUNCT:
                j -= 1
            end = j

        end = max(start, min(n, end))
        if end == start and start < n:
            end = start + 1
        spans.append((start, end))
        floor = end
    return spans


def even_timings(
    spans: Sequence[tuple[int, int]], duration: float, nslots: int | None = None
) -> list[Timed]:
    """Degraded fallback: spread `duration` over the slots by character width.

    Used when real alignment is impossible or produced nonsense.  Reading is
    still usable (the highlight sweeps at roughly the right pace); only the
    per-word precision is lost.
    """
    n = len(spans) if nslots is None else int(nslots)
    if n <= 0:
        return []
    dur = max(0.0, float(duration))
    widths = [
        max(1, (spans[i][1] - spans[i][0]) if i < len(spans) else 1) for i in range(n)
    ]
    total_w = float(sum(widths)) or 1.0
    out: list[Timed] = []
    t = 0.0
    for i, w in enumerate(widths):
        step = dur * (w / total_w)
        out.append(Timed(i, t, min(dur, t + step)))
        t = min(dur, t + step)
    return out


def _sanitize(timings: list[Timed], nslots: int, duration: float) -> list[Timed]:
    """Clamp into [0, duration] and enforce the contract's monotonicity.

    Raises ValueError when the alignment is structurally wrong (wrong number of
    slots, wrong slot ids), which is the signal to fall back to `even_timings`.
    """
    if len(timings) != nslots:
        raise ValueError(f"alignment produced {len(timings)} timings for {nslots} slots")
    hi = max(0.0, float(duration))
    run = 0.0
    out: list[Timed] = []
    for i, t in enumerate(timings):
        if int(t.word_slot) != i:
            raise ValueError(f"alignment slot {t.word_slot!r} out of order at {i}")
        s = float(t.start)
        e = float(t.end)
        if s != s or e != e:  # NaN
            raise ValueError("alignment produced NaN timings")
        if hi > 0.0:
            s = min(max(0.0, s), hi)
            e = min(max(0.0, e), hi)
        else:
            s = max(0.0, s)
            e = max(0.0, e)
        s = max(run, s)
        e = max(s, e)
        out.append(Timed(i, s, e))
        run = s
    return out


def build_timings(
    text: str,
    spans: Sequence[tuple[int, int]],
    results: Iterable[tuple[Sequence[Any], float]],
    duration: float,
    nslots: int,
) -> list[Timed]:
    """Align, validate and clamp -- degrading to `even_timings` on any failure.

    This is the single place `Engine.synth` turns pipeline output into `Timed`s,
    factored out so it can be exercised (and sabotaged) without the model.
    """
    if nslots <= 0:
        return []
    try:
        raw = align_words(text, spans, results)
        timings = [Timed(int(s), float(a), float(b)) for s, a, b in raw]
        return _sanitize(timings, nslots, duration)
    except Exception:  # noqa: BLE001 - alignment must never break the reader
        return even_timings(spans, duration, nslots)

