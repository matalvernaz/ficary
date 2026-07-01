"""Soundscape subsystem: ambient audio beds for the in-app reader.

Definitions are JSON files in ``portable.soundscapes_dir()``; a soundscape is
assigned to a story by ``story_key`` in ``reader-state.db``. Playback goes
through the shared :mod:`ficary.audio.engine` ambient channel, so importing
this package never requires the audio engine or OpenAL.
"""
