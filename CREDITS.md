# Credits

## Sounds

Both recordings are from Freesound and are in the public domain under CC0. The page plays the edited MP3s in `src/second_screen_pundit/web/audio/`.

| Used as | Title | Author | Source | Licence |
| :-- | :-- | :-- | :-- | :-- |
| Crowd bed (`crowd-bed.mp3`) | Norwegian football match / soccer game ambience XY to ORTF, AB and Binaural | Vogyik | https://freesound.org/people/Vogyik/sounds/868982/ | CC0 1.0 |
| Goal roar (`goal-roar.mp3`) | 19 Football Crowd - Reaction To Goal.flac | D.jones | https://freesound.org/people/D.jones/sounds/528799/ | CC0 1.0 |

**Edits:**
- **Crowd bed:** the four takes (one per microphone setup) are cut to their steady middle 18 seconds and joined with 2-second equal-power crossfades. The silent gaps and the fades between takes are removed. The result is 66 seconds at 112 kbps.
- **Goal roar:** the first 14 seconds, with a 4-second fade-out, at 112 kbps.

The referee's whistles are synthesised in the browser and use no recording.

## Code

The evidence check's number extraction is adapted from Brian Benz's MIT-licensed [football-results-dashboard `grounding.py`](https://github.com/bbenz/football-results-dashboard/blob/main/agent/grounding.py). Its licence is kept in `third_party/bbenz_grounding/`.
