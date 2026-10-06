# English-content episode workflow

The learner provides a video URL or identifies an episode to watch. Prepare a
short pre-viewing note from the actual English transcript.

1. For a video URL, call `fetch_subtitles` with `lang: en`. It downloads only a
   subtitle track, including auto-captions when available. For a transcript web
   page, call `fetch_page`.
2. Verify that the text belongs to the requested video or episode. Check the
   title and opening context. A search snippet or plot summary is not a transcript.
3. If no matching transcript is available, say so and stop. Do not invent lines
   or vocabulary from memory.
4. Select 12-20 expressions that genuinely occur in the text. Prefer reusable
   phrases and terms appropriate to the learner's level and the content genre.
5. Send a note the learner can read before watching. After viewing, ask which
   expressions were understood, heard but not understood, or missed; only the
   latter two groups should enter spaced repetition.

The complete personal tutor also uses learner-specific memory, Telegram and SRS.
Those private integrations and data are outside this public code slice.
