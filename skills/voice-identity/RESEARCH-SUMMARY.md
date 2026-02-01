# Voice Identity Research Summary

**Goal:** Enable programmatic evaluation of voice samples to iterate toward a target voice identity.

## Key Findings

### 1. Audio Features and Perceptual Meanings

The following features can be extracted and mapped to subjective voice qualities:

| Feature | What It Measures | Perceptual Mapping |
|---------|-----------------|-------------------|
| **Spectral Centroid** | Frequency "center of mass" | **Brightness/warmth** — higher = brighter, lower = warmer |
| **RMS Energy** | Amplitude/loudness | **Intensity/confidence** — higher = more forceful |
| **Spectral Flatness** | Noise vs tone ratio | **Texture** — higher = raspier/grainier |
| **Zero Crossing Rate** | Signal sign changes | **Breathiness** — higher = more airy |
| **MFCCs** | Timbre fingerprint | Voice identity matching (not directly interpretable) |
| **F0 (Pitch)** | Fundamental frequency | Perceived "highness/lowness" of voice |

### 2. Mapping to Subjective Qualities

Your target qualities can be approximated by:

| Quality | Feature Targets |
|---------|----------------|
| **"Intense but not aggressive"** | High RMS + moderate spectral centroid + controlled pitch variability |
| **"Clear and direct"** | High spectral contrast + consistent energy + moderate-high centroid |
| **"Warmth with an edge"** | Low-mid centroid (1500-2500 Hz) + some spectral flatness |
| **"Confident without being deep"** | Moderate F0 + steady pitch + strong RMS |

### 3. ElevenLabs Voice Design Parameters

**For creating new voices (Voice Design):**
- Use detailed text prompts describing age, accent, tone, timbre, pacing
- Include audio quality descriptors ("perfect audio quality", "studio-quality")
- **Guidance Scale**: Higher = more prompt-accurate (but potentially lower quality if prompt is unusual)

**For tuning existing voices (Voice Settings):**

| Parameter | Effect |
|-----------|--------|
| `stability` (0-1) | Low = more emotional variation, High = more monotone |
| `similarity_boost` (0-1) | How closely to match original voice |
| `style` (0-1) | Exaggerates speaker's style |
| `speed` (0.5-2) | Speaking rate |

### 4. Feasibility of Feedback Loop

**Yes, a feedback loop is feasible**, but with caveats:

```
[Define Target] → [Generate Voice] → [Analyze Features] → [Compare to Target] → [Adjust Prompt/Settings] → [Repeat]
```

**Challenges:**
1. **Subjective ↔ Objective Gap**: Feature mappings are approximations
2. **ElevenLabs is a black box**: Can't directly control audio features
3. **Context matters**: Same features sound different based on content
4. **Manual iteration**: No automated prompt adjustment (yet)

### 5. Proof of Concept Delivered

Created working tools at `/home/node/clawd/skills/voice-identity/`:

| Tool | Purpose |
|------|---------|
| `analyze.ts` | Extract features from WAV file → JSON profile |
| `compare.ts` | Compare two profiles, get recommendations |
| `SKILL.md` | Full documentation of features and mappings |

**Example output:**
```json
{
  "pitch": { "estimated_f0_hz": 145, "interpretation": "typical male range" },
  "brightness": { "spectral_centroid_hz": 2340, "interpretation": "clear, present" },
  "intensity": { "rms_db": -18.2, "interpretation": "moderate, conversational" },
  "texture": { "spectral_flatness": 0.15, "interpretation": "slight edge/grain" },
  "recommendations": ["Voice is well-balanced - adjust stability/style for fine-tuning"]
}
```

## Recommendations

### Immediate Next Steps

1. **Get ElevenLabs API key** — not currently in environment (`$ELEVENLABS_API_KEY` not set)
2. **Generate sample voices** with different prompts and analyze them
3. **Create a reference library** of analyzed voices with known qualities
4. **Build comparison workflow**: target profile → generate → analyze → iterate

### Future Improvements

1. **Automated prompt mutation** based on feature gaps
2. **Real F0 extraction** using YIN or autocorrelation (current estimate is rough)
3. **Batch analysis** across multiple samples for stability
4. **ElevenLabs integration** for end-to-end generate → analyze loop

## Environment Notes

- **Python/librosa**: Not available (no pip in container)
- **Bun/Meyda**: Working solution implemented
- **ElevenLabs API**: Key not configured
- **FFmpeg**: Not installed (would need for MP3/other format support)

## References

- [librosa feature docs](https://librosa.org/doc/latest/feature.html)
- [ElevenLabs Voice Design](https://elevenlabs.io/docs/creative-platform/voices/voice-design)
- [ElevenLabs Voice Settings](https://elevenlabs.io/docs/api-reference/voices/settings/get)
