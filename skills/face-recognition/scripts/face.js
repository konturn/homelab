#!/usr/bin/env node

const path = require('path');
const fs = require('fs');
const tf = require('@tensorflow/tfjs-node');
const canvas_pkg = require('canvas');
const faceapi = require('@vladmandic/face-api');

// Setup canvas environment for face-api
const { Canvas, Image, ImageData } = canvas_pkg;
faceapi.env.monkeyPatch({ Canvas, Image, ImageData });

// Configuration
const MODELS_PATH = path.join(__dirname, '..', 'models');
const PEOPLE_BASE_PATH = '/home/node/.openclaw/workspace/life/areas/people';
const CONFIDENCE_THRESHOLD = 0.6;
const STRONG_MATCH_THRESHOLD = 0.4;

// Global variables for loaded models
let modelsLoaded = false;

// Error handling for unhandled promises
process.on('unhandledRejection', (reason, promise) => {
  console.error('Unhandled Rejection:', reason);
  process.exit(1);
});

/**
 * Load face-api models
 */
async function loadModels() {
  if (modelsLoaded) return;
  
  try {
    console.log('Loading face-api models...');
    await faceapi.nets.ssdMobilenetv1.loadFromDisk(MODELS_PATH);
    await faceapi.nets.faceLandmark68Net.loadFromDisk(MODELS_PATH);
    await faceapi.nets.faceRecognitionNet.loadFromDisk(MODELS_PATH);
    modelsLoaded = true;
    console.log('Models loaded successfully');
  } catch (error) {
    console.error('Failed to load models:', error);
    throw error;
  }
}

/**
 * Load image from file path using canvas
 */
async function loadImage(imagePath) {
  try {
    const img = new Image();
    const buffer = fs.readFileSync(imagePath);
    img.src = buffer;
    return img;
  } catch (error) {
    throw new Error(`Failed to load image from ${imagePath}: ${error.message}`);
  }
}

/**
 * Extract face descriptor (128-dim embedding) from image
 */
async function extractFaceDescriptor(imagePath) {
  const img = await loadImage(imagePath);
  
  const detections = await faceapi
    .detectAllFaces(img)
    .withFaceLandmarks()
    .withFaceDescriptors();

  if (detections.length === 0) {
    throw new Error('No face detected in image');
  }

  if (detections.length > 1) {
    console.warn(`Warning: ${detections.length} faces detected, using the first one`);
  }

  const detection = detections[0];
  return {
    descriptor: Array.from(detection.descriptor),
    confidence: detection.detection.score,
    box: detection.detection.box
  };
}

/**
 * Calculate Euclidean distance between two descriptors
 */
function calculateDistance(desc1, desc2) {
  if (desc1.length !== desc2.length) {
    throw new Error('Descriptor dimensions do not match');
  }
  
  let sum = 0;
  for (let i = 0; i < desc1.length; i++) {
    const diff = desc1[i] - desc2[i];
    sum += diff * diff;
  }
  return Math.sqrt(sum);
}

/**
 * Save face embedding to knowledge graph
 */
async function saveFaceEmbedding(name, descriptor, imagePath, confidence) {
  const personDir = path.join(PEOPLE_BASE_PATH, name.toLowerCase());
  
  // Ensure person directory exists
  if (!fs.existsSync(personDir)) {
    fs.mkdirSync(personDir, { recursive: true });
  }
  
  const embeddingData = {
    name: name,
    registeredAt: new Date().toISOString(),
    descriptor: descriptor,
    imageSource: imagePath,
    confidence: confidence
  };
  
  const embeddingPath = path.join(personDir, 'face_embedding.json');
  fs.writeFileSync(embeddingPath, JSON.stringify(embeddingData, null, 2));
  
  return embeddingPath;
}

/**
 * Load all stored face embeddings from knowledge graph
 */
function loadAllEmbeddings() {
  const embeddings = [];
  
  try {
    if (!fs.existsSync(PEOPLE_BASE_PATH)) {
      return embeddings;
    }
    
    const peopleDirectories = fs.readdirSync(PEOPLE_BASE_PATH);
    
    for (const personDir of peopleDirectories) {
      const personPath = path.join(PEOPLE_BASE_PATH, personDir);
      const embeddingPath = path.join(personPath, 'face_embedding.json');
      
      if (fs.existsSync(embeddingPath) && fs.statSync(personPath).isDirectory()) {
        try {
          const embeddingData = JSON.parse(fs.readFileSync(embeddingPath, 'utf8'));
          embeddings.push({
            ...embeddingData,
            path: embeddingPath
          });
        } catch (error) {
          console.warn(`Failed to load embedding for ${personDir}: ${error.message}`);
        }
      }
    }
  } catch (error) {
    console.error('Failed to load embeddings:', error);
  }
  
  return embeddings;
}

/**
 * Register a new face
 */
async function registerFace(name, imagePath) {
  try {
    await loadModels();
    
    if (!fs.existsSync(imagePath)) {
      throw new Error(`Image file not found: ${imagePath}`);
    }
    
    console.log(`Registering face for ${name} from ${imagePath}...`);
    
    const result = await extractFaceDescriptor(imagePath);
    const embeddingPath = await saveFaceEmbedding(name, result.descriptor, imagePath, result.confidence);
    
    console.log(`Face registered successfully!`);
    console.log(`Name: ${name}`);
    console.log(`Confidence: ${result.confidence.toFixed(3)}`);
    console.log(`Embedding saved to: ${embeddingPath}`);
    
    return {
      success: true,
      name,
      confidence: result.confidence,
      embeddingPath
    };
  } catch (error) {
    console.error(`Registration failed: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * Identify faces in an image
 */
async function identifyFaces(imagePath) {
  try {
    await loadModels();
    
    if (!fs.existsSync(imagePath)) {
      throw new Error(`Image file not found: ${imagePath}`);
    }
    
    console.log(`Identifying faces in ${imagePath}...`);
    
    // Extract descriptor from target image
    const targetResult = await extractFaceDescriptor(imagePath);
    const targetDescriptor = targetResult.descriptor;
    
    // Load all stored embeddings
    const storedEmbeddings = loadAllEmbeddings();
    
    if (storedEmbeddings.length === 0) {
      console.log('No registered faces found');
      return { success: true, matches: [] };
    }
    
    // Compare against all stored embeddings
    const matches = [];
    for (const embedding of storedEmbeddings) {
      const distance = calculateDistance(targetDescriptor, embedding.descriptor);
      
      if (distance < CONFIDENCE_THRESHOLD) {
        matches.push({
          name: embedding.name,
          distance: distance,
          similarity: 1 - distance,
          confidence: distance < STRONG_MATCH_THRESHOLD ? 'strong' : 'weak',
          registeredAt: embedding.registeredAt
        });
      }
    }
    
    // Sort by distance (best matches first)
    matches.sort((a, b) => a.distance - b.distance);
    
    if (matches.length > 0) {
      console.log(`Found ${matches.length} match(es):`);
      for (const match of matches) {
        console.log(`  ${match.name} (distance: ${match.distance.toFixed(3)}, confidence: ${match.confidence})`);
      }
    } else {
      console.log('No matching faces found');
    }
    
    return {
      success: true,
      matches,
      targetConfidence: targetResult.confidence
    };
  } catch (error) {
    console.error(`Identification failed: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * Compare two images directly
 */
async function compareImages(imagePath1, imagePath2) {
  try {
    await loadModels();
    
    if (!fs.existsSync(imagePath1)) {
      throw new Error(`Image file not found: ${imagePath1}`);
    }
    if (!fs.existsSync(imagePath2)) {
      throw new Error(`Image file not found: ${imagePath2}`);
    }
    
    console.log(`Comparing ${imagePath1} and ${imagePath2}...`);
    
    const result1 = await extractFaceDescriptor(imagePath1);
    const result2 = await extractFaceDescriptor(imagePath2);
    
    const distance = calculateDistance(result1.descriptor, result2.descriptor);
    const similarity = 1 - distance;
    
    const isMatch = distance < CONFIDENCE_THRESHOLD;
    const confidence = distance < STRONG_MATCH_THRESHOLD ? 'strong' : (isMatch ? 'weak' : 'no match');
    
    console.log(`Comparison result:`);
    console.log(`  Distance: ${distance.toFixed(3)}`);
    console.log(`  Similarity: ${similarity.toFixed(3)}`);
    console.log(`  Match: ${isMatch ? 'Yes' : 'No'} (${confidence})`);
    
    return {
      success: true,
      distance,
      similarity,
      isMatch,
      confidence,
      image1Confidence: result1.confidence,
      image2Confidence: result2.confidence
    };
  } catch (error) {
    console.error(`Comparison failed: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * List all registered faces
 */
function listRegisteredFaces() {
  try {
    console.log('Scanning for registered faces...');
    
    const embeddings = loadAllEmbeddings();
    
    if (embeddings.length === 0) {
      console.log('No registered faces found');
      return { success: true, faces: [] };
    }
    
    console.log(`Found ${embeddings.length} registered face(s):`);
    
    const faces = embeddings.map(embedding => ({
      name: embedding.name,
      registeredAt: embedding.registeredAt,
      imageSource: embedding.imageSource,
      confidence: embedding.confidence
    }));
    
    // Sort by registration date
    faces.sort((a, b) => new Date(a.registeredAt) - new Date(b.registeredAt));
    
    for (const face of faces) {
      const date = new Date(face.registeredAt).toLocaleDateString();
      console.log(`  ${face.name} (registered: ${date}, confidence: ${face.confidence.toFixed(3)})`);
    }
    
    return { success: true, faces };
  } catch (error) {
    console.error(`List failed: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * Show usage help
 */
function showHelp() {
  console.log('Face Recognition Tool');
  console.log('');
  console.log('Usage:');
  console.log('  node scripts/face.js register <name> <image_path>');
  console.log('  node scripts/face.js identify <image_path>');
  console.log('  node scripts/face.js compare <image1> <image2>');
  console.log('  node scripts/face.js list');
  console.log('');
  console.log('Commands:');
  console.log('  register  Register a new face with a name');
  console.log('  identify  Identify face(s) in an image');
  console.log('  compare   Compare faces in two images');
  console.log('  list      List all registered faces');
  console.log('');
  console.log('Examples:');
  console.log('  node scripts/face.js register "John Doe" /path/to/john.jpg');
  console.log('  node scripts/face.js identify /path/to/unknown.jpg');
  console.log('  node scripts/face.js compare photo1.jpg photo2.jpg');
  console.log('');
  console.log('Confidence thresholds:');
  console.log(`  Strong match: distance < ${STRONG_MATCH_THRESHOLD}`);
  console.log(`  Weak match: distance < ${CONFIDENCE_THRESHOLD}`);
  console.log(`  No match: distance >= ${CONFIDENCE_THRESHOLD}`);
}

/**
 * Main CLI interface
 */
async function main() {
  const args = process.argv.slice(2);
  
  if (args.length === 0 || args[0] === 'help' || args[0] === '--help' || args[0] === '-h') {
    showHelp();
    return;
  }
  
  const command = args[0].toLowerCase();
  
  try {
    switch (command) {
      case 'register':
        if (args.length < 3) {
          console.error('Error: register requires <name> and <image_path>');
          console.error('Usage: node scripts/face.js register <name> <image_path>');
          process.exit(1);
        }
        await registerFace(args[1], args[2]);
        break;
        
      case 'identify':
        if (args.length < 2) {
          console.error('Error: identify requires <image_path>');
          console.error('Usage: node scripts/face.js identify <image_path>');
          process.exit(1);
        }
        await identifyFaces(args[1]);
        break;
        
      case 'compare':
        if (args.length < 3) {
          console.error('Error: compare requires <image1> and <image2>');
          console.error('Usage: node scripts/face.js compare <image1> <image2>');
          process.exit(1);
        }
        await compareImages(args[1], args[2]);
        break;
        
      case 'list':
        listRegisteredFaces();
        break;
        
      default:
        console.error(`Unknown command: ${command}`);
        console.error('Run "node scripts/face.js help" for usage information');
        process.exit(1);
    }
  } catch (error) {
    console.error('Fatal error:', error);
    process.exit(1);
  }
}

// Run the CLI
if (require.main === module) {
  main();
}

module.exports = {
  loadModels,
  extractFaceDescriptor,
  registerFace,
  identifyFaces,
  compareImages,
  listRegisteredFaces,
  calculateDistance
};