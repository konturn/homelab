# Face Recognition Skill

Advanced face recognition system using deep learning to register faces, extract 128-dimensional embeddings, and identify people from photos.

## Overview

This skill provides face detection, recognition, and comparison capabilities using:
- **@vladmandic/face-api** — Modern face detection and embedding extraction
- **@tensorflow/tfjs-node** — Native TensorFlow backend for performance
- **canvas** — Image loading and processing without browser dependencies

Face embeddings are stored in the knowledge graph at `/life/areas/people/<name>/face_embedding.json` for persistent person tracking.

## Quick Start

```bash
# Navigate to skill directory
cd /home/node/.openclaw/workspace/skills/face-recognition

# Register a new face
node scripts/face.js register "Avery" /path/to/avery_photo.jpg

# Identify faces in an unknown image
node scripts/face.js identify /path/to/mystery_person.jpg

# Compare two images directly
node scripts/face.js compare photo1.jpg photo2.jpg

# List all registered faces
node scripts/face.js list
```

## Commands

### `register <name> <image_path>`

Register a new face in the system.

- **name**: Person's name (will create `/life/areas/people/<name>/` directory)
- **image_path**: Path to image file containing the person's face

**Example:**
```bash
node scripts/face.js register "John Doe" ~/photos/john_headshot.jpg
```

**Output:**
- Detects face in image
- Extracts 128-dimensional embedding
- Saves to `face_embedding.json` in person's knowledge graph folder
- Reports confidence score

### `identify <image_path>`

Identify faces in an image by comparing against all registered embeddings.

- **image_path**: Path to image containing unknown face(s)

**Example:**
```bash
node scripts/face.js identify ~/photos/group_photo.jpg
```

**Output:**
- Lists all matching registered faces
- Shows distance metrics and confidence levels
- Handles multiple faces in one image

### `compare <image1> <image2>`

Direct comparison between two face images.

- **image1**: Path to first image
- **image2**: Path to second image

**Example:**
```bash
node scripts/face.js compare photo_a.jpg photo_b.jpg
```

**Output:**
- Euclidean distance between face embeddings
- Similarity score (1 - distance)
- Match determination based on thresholds

### `list`

List all registered faces in the knowledge graph.

**Example:**
```bash
node scripts/face.js list
```

**Output:**
- Shows all people with stored face embeddings
- Registration dates and confidence scores
- Sorted chronologically

## How Face Embeddings Work

### Storage Format

Face embeddings are stored as JSON files in the knowledge graph:

```json
{
  "name": "avery",
  "registeredAt": "2026-02-04T06:53:00.000Z",
  "descriptor": [0.123, -0.456, 0.789, ...],  // 128 floats
  "imageSource": "/path/to/original.jpg",
  "confidence": 0.98
}
```

**Storage Location:** `/life/areas/people/<name>/face_embedding.json`

### Face Detection Pipeline

1. **Image Loading** — Canvas-based image loading (no browser required)
2. **Face Detection** — SSD MobileNetV1 detects face boundaries
3. **Landmark Detection** — 68-point facial landmark identification
4. **Embedding Extraction** — 128-dimensional face descriptor generation
5. **Storage** — Embedding saved to knowledge graph with metadata

### Distance Metrics & Confidence Thresholds

Face similarity uses **Euclidean distance** between 128-dim embeddings:

- **Strong Match**: `distance < 0.4` — High confidence same person
- **Weak Match**: `distance < 0.6` — Probable match, some uncertainty
- **No Match**: `distance >= 0.6` — Different people

Lower distance = higher similarity. Typical ranges:
- Same person (good photos): 0.2 - 0.4
- Same person (poor lighting/angle): 0.4 - 0.6
- Different people: 0.6+

## Error Handling

The script handles common issues gracefully:

- **No face detected** — Image doesn't contain recognizable faces
- **Multiple faces** — Uses first detected face, warns about others
- **File not found** — Clear error for missing image files
- **Blurry/low quality** — Low confidence scores indicate quality issues
- **Missing models** — Automatic model loading with clear error messages

## Model Files

Pre-trained models are included with the @vladmandic/face-api package:

- **ssd_mobilenetv1** — Face detection (5.6 MB)
- **face_landmark_68** — Facial landmarks (357 KB)  
- **face_recognition** — Embedding extraction (6.4 MB)

Models are copied to `skills/face-recognition/models/` for faster access.

## Integration Examples

### Register from Camera
```bash
# After capturing photo with camera skill
node scripts/face.js register "Guest" /tmp/camera_capture.jpg
```

### Bulk Registration
```bash
# Register multiple family members
for photo in family_photos/*.jpg; do
    name=$(basename "$photo" .jpg)
    node scripts/face.js register "$name" "$photo"
done
```

### Automated Identification
```bash
# Identify person in security camera capture
result=$(node scripts/face.js identify /tmp/doorbell_snapshot.jpg)
if echo "$result" | grep -q "Found.*match"; then
    echo "Known person detected"
else
    echo "Unknown visitor"
fi
```

## Performance Notes

- **First run** takes ~3-5 seconds for model loading
- **Subsequent operations** are much faster (~1-2 seconds)
- **Memory usage** ~500MB with models loaded
- **Accuracy** depends on image quality, lighting, face angle

## Troubleshooting

### "No face detected"
- Ensure image contains clear, front-facing face
- Check image isn't rotated or heavily cropped
- Try different lighting conditions

### Low confidence scores
- Use high-resolution images when possible
- Ensure good lighting without harsh shadows
- Front-facing poses work best

### "Models failed to load"
- Check that model files exist in `models/` directory
- Verify TensorFlow backend is working: `node -e "console.log(require('@tensorflow/tfjs-node').version)"`

## Dependencies

Required packages (already installed):
```json
{
  "@tensorflow/tfjs-node": "^4.22.0",
  "@vladmandic/face-api": "^1.7.15", 
  "canvas": "^3.2.1"
}
```

All model files are included with the face-api package, no external downloads required.

---

**Pro tip:** For best results, register faces using clear, well-lit photos taken from similar angles/distances you expect during identification.