# Voice Identity Analysis Skill

## Overview

Tools and knowledge for analyzing voice samples and iterating toward a target voice identity. This enables programmatic evaluation of TTS output to guide voice design decisions.

## Audio Features → Perceptual Qualities

### Core Features (extractable with librosa/Meyda)

| Feature | What It Is | Perceptual Meaning |
|---------|-----------|-------------------|
| **Pitch (F0)** | Fundamental frequency | How "high" or "low" the voice sounds. Lower = deeper, authoritative. Higher = younger, energetic. |
| **Pitch Variability (F0 std)** | Standard deviation of pitch | Expressiveness. Low = monotone/controlled. High = animated/emotional. |
| **Spectral Centroid** | "Center of mass" of frequencies | **Brightness**. Higher = brighter/sharper. Lower = warmer/darker. |
| **Spectral Rolloff** | Frequency below which 85% of energy sits | Similar to centroid but captures high-freq "air" or "sizzle". |
| **MFCCs** | Mel-frequency cepstral coefficients | Timbre fingerprint. Used for voice matching, not direct interpretation. |
| **RMS Energy** | Root-mean-square amplitude | **Loudness/intensity**. Higher = more forceful. |
| **Zero Crossing Rate** | How often signal crosses zero | Noisiness. Higher = breathier or more percussive. |
| **Spectral Flatness** | How "noise-like" vs "tonal" | Gravelly/raspy voices have higher flatness. |
| **Speaking Rate** | Syllables/words per second | Pace. Fast = urgent/energetic. Slow = deliberate/calming. |

### Mapping Features to Subjective Qualities

| Desired Quality | Key Features | Target Range |
|----------------|--------------|--------------|
| **"Intense but not aggressive"** | High RMS + moderate pitch variability + controlled spectral centroid | RMS: 75th percentile, F0_std: mid-range, centroid: mid-high |
| **"Clear and direct"** | Low zero-crossing + high spectral contrast + consistent RMS | ZCR: low, contrast: high, RMS_std: low |
| **"Warmth with an edge"** | Low-mid spectral centroid + some spectral flatness + moderate pitch | Centroid: 1500-2500 Hz, flatness: 0.1-0.3 |
| **"Confident without being deep"** | Moderate F0 (not bass) + low pitch variability + high RMS | F0: 120-180 Hz (male), steady, strong |

### Frequency Bands and Voice Qualities

| Range | Frequency | Effect on Voice |
|-------|-----------|-----------------|
| **Sub-bass** | 20-60 Hz | Rumble, usually unwanted |
| **Bass** | 60-250 Hz | Body, depth, "chestiness" |
| **Low-mid** | 250-500 Hz | Warmth, fullness |
| **Mid** | 500-2000 Hz | Clarity, presence |
| **Upper-mid** | 2000-4000 Hz | Edge, intelligibility, "bite" |
| **Presence** | 4000-6000 Hz | Clarity, definition |
| **Brilliance** | 6000-20000 Hz | Air, sibilance, sparkle |

---

## ElevenLabs Voice Design

### Voice Design Prompt (Text-to-Voice)

When creating a new voice from scratch, ElevenLabs uses a **text prompt** to generate the voice. Key attributes:

- **Age**: "young", "middle-aged", "elderly", "in his 40s"
- **Gender**: "male", "female", "gender-neutral"
- **Accent**: "thick British", "slight Southern", "neutral American"
- **Tone/Timbre**: "deep", "warm", "gravelly", "smooth", "raspy", "breathy"
- **Pacing**: "fast-paced", "slow", "deliberate", "conversational"
- **Character**: "confident", "sarcastic", "energetic", "calm"
- **Audio Quality**: "perfect audio quality", "studio-quality"

**Guidance Scale** (0-100%): How strictly to follow the prompt. Higher = more accurate but potentially lower quality if prompt is unusual.

### Voice Settings (TTS Parameters)

When using an existing voice, these settings modify generation:

| Parameter | Range | Effect |
|-----------|-------|--------|
| **stability** | 0-1 | Low = more emotional variation. High = more monotone/consistent. |
| **similarity_boost** | 0-1 | How closely to match the original voice sample. |
| **style** | 0-1 | Exaggerates the speaker's style. Higher = more dramatic. |
| **speed** | 0.5-2.0 | Playback speed. 1.0 = normal. |
| **use_speaker_boost** | bool | Enhances similarity to original (adds latency). |

### Feedback Loop Strategy

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  1. Define target voice profile (subjective qualities)     │
│                         ↓                                   │
│  2. Map qualities to feature targets                       │
│                         ↓                                   │
│  3. Generate voice sample (ElevenLabs)                     │
│                         ↓                                   │
│  4. Extract features (analyze.ts)                          │
│                         ↓                                   │
│  5. Compare to targets → compute "voice score"             │
│                         ↓                                   │
│  6. Adjust prompts/settings based on gaps                  │
│                         ↓                                   │
│  7. Repeat until satisfactory                              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Usage

### Analyze a voice sample

```bash
cd /home/node/clawd/skills/voice-identity
bun run analyze.ts /path/to/audio.wav
```

### Output: Voice Profile

```json
{
  "pitch": {
    "mean_hz": 145.2,
    "std_hz": 23.4,
    "interpretation": "mid-range male, moderate expressiveness"
  },
  "brightness": {
    "spectral_centroid_hz": 2340,
    "interpretation": "clear, slightly bright"
  },
  "intensity": {
    "rms_db": -18.2,
    "interpretation": "moderate energy"
  },
  "texture": {
    "zcr": 0.042,
    "flatness": 0.15,
    "interpretation": "smooth with slight edge"
  },
  "summary": "Confident mid-range voice with clarity. Could increase warmth by lowering centroid."
}
```

---

## Limitations

1. **Subjective ↔ Objective Gap**: "Warmth" and "intensity" are perceptual—feature mappings are approximations.
2. **Context Matters**: Same features can sound different based on content/emotion.
3. **ElevenLabs is a Black Box**: Can't directly control audio features, only prompt and settings.
4. **Iteration is Manual**: Currently no automated adjustment loop—you interpret and adjust.

---

## Future Improvements

- [ ] Automated prompt mutation based on feature gaps
- [ ] Reference voice comparison (compute distance to target sample)
- [ ] Real-time analysis during generation
- [ ] Integration with ElevenLabs API for end-to-end iteration

---

## References

- [librosa documentation](https://librosa.org/doc/latest/feature.html)
- [ElevenLabs Voice Design](https://elevenlabs.io/docs/creative-platform/voices/voice-design)
- [ElevenLabs Voice Settings](https://elevenlabs.io/docs/api-reference/voices/settings/get)
