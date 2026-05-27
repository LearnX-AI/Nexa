#!/usr/bin/env python3
"""
Retrieval Testing Module
Tests the retrieval and filtering functionality with generated embeddings.
"""

import json
import numpy as np
from pathlib import Path
from typing import List, Dict
from sentence_transformers import SentenceTransformer


class RetrievalSystem:
    """Simple retrieval system using semantic search with metadata filtering."""
    
    def __init__(self, chunks_path: Path, embeddings_path: Path):
        """Initialize retrieval system with chunks and embeddings."""
        self.chunks_path = chunks_path
        self.embeddings_path = embeddings_path
        self.chunks = []
        self.embeddings = []
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        
        self._load_data()
    
    def _load_data(self):
        """Load chunks and embeddings from disk."""
        # Load chunks
        with open(self.chunks_path, 'r') as f:
            for line in f:
                self.chunks.append(json.loads(line))
        
        # Load embeddings
        with open(self.embeddings_path, 'r') as f:
            for line in f:
                data = json.loads(line)
                self.embeddings.append(np.array(data['embedding']))
        
        print(f"✓ Loaded {len(self.chunks)} chunks with {len(self.embeddings)} embeddings")
    
    def teacher_filter(self, chunk: Dict) -> bool:
        """Filter for teacher-facing content."""
        audience = chunk['metadata'].get('audience', 'both')
        return audience in ['teacher', 'both']
    
    def student_filter(self, chunk: Dict, grade_level: int) -> bool:
        """Filter for student content at given grade level."""
        audience = chunk['metadata'].get('audience', 'both')
        grade_min = chunk['metadata'].get('grade_level_min')
        grade_max = chunk['metadata'].get('grade_level_max')
        
        # Audience check
        if audience not in ['student', 'both']:
            return False
        
        # Grade level check (lenient if not specified)
        if grade_min is not None and grade_level < grade_min:
            return False
        if grade_max is not None and grade_level > grade_max:
            return False
        
        return True
    
    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        audience: str = 'both',
        grade_level: int = None
    ) -> List[Dict]:
        """Retrieve top-k most relevant chunks with filtering."""
        # Encode query
        query_embedding = self.model.encode(query)
        
        # Compute similarities
        similarities = []
        for i, embedding in enumerate(self.embeddings):
            sim = np.dot(query_embedding, embedding)
            similarities.append((i, sim))
        
        # Sort by similarity
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        # Filter and return top-k
        results = []
        for idx, similarity in similarities:
            chunk = self.chunks[idx]
            
            # Apply filters
            if audience == 'teacher':
                if not self.teacher_filter(chunk):
                    continue
            elif audience == 'student':
                if not self.student_filter(chunk, grade_level or 10):
                    continue
            
            results.append({
                'id': chunk['id'],
                'text': chunk['text'][:200] + '...' if len(chunk['text']) > 200 else chunk['text'],
                'similarity': float(similarity),
                'metadata': {
                    'source': chunk['metadata']['source_file'].split('/')[-1],
                    'audience': chunk['metadata'].get('audience'),
                    'type': chunk['metadata'].get('element_type'),
                }
            })
            
            if len(results) >= top_k:
                break
        
        return results


def test_retrieval():
    """Test retrieval system with sample queries."""
    print("\n" + "="*70)
    print("RETRIEVAL AND FILTERING TEST")
    print("="*70)
    
    # Test both output directories
    test_cases = [
        {
            'name': 'General Audience (No Filters)',
            'chunks': Path('local_pipeline_output/chunks.jsonl'),
            'embeddings': Path('local_pipeline_output/embeddings.jsonl'),
            'queries': [
                ('photosynthesis reactions', 'both', None),
                ('light energy conversion', 'both', None),
            ]
        },
        {
            'name': 'Teacher Audience (Grades 10-12)',
            'chunks': Path('local_pipeline_teacher_output/chunks.jsonl'),
            'embeddings': Path('local_pipeline_teacher_output/embeddings.jsonl'),
            'queries': [
                ('photosynthesis learning objectives', 'teacher', 11),
                ('teaching key concepts', 'teacher', 12),
            ]
        }
    ]
    
    for test_case in test_cases:
        print(f"\n\n{'─'*70}")
        print(f"TEST CASE: {test_case['name']}")
        print(f"{'─'*70}")
        
        if not test_case['chunks'].exists():
            print(f"⚠️  Chunks file not found: {test_case['chunks']}")
            continue
        
        retrieval = RetrievalSystem(test_case['chunks'], test_case['embeddings'])
        
        for query, audience, grade in test_case['queries']:
            print(f"\n📝 Query: \"{query}\"")
            print(f"   Audience: {audience}, Grade Level: {grade}")
            print(f"   {'-'*66}")
            
            results = retrieval.retrieve(query, top_k=2, audience=audience, grade_level=grade)
            
            if not results:
                print("   ❌ No results found")
                continue
            
            for i, result in enumerate(results, 1):
                print(f"\n   Result {i}:")
                print(f"   ID: {result['id']}")
                print(f"   Similarity Score: {result['similarity']:.4f}")
                print(f"   Source: {result['metadata']['source']}")
                print(f"   Audience: {result['metadata']['audience']}")
                print(f"   Type: {result['metadata']['type']}")
                print(f"   Text: {result['text']}")


def compare_outputs():
    """Compare the two pipeline outputs."""
    print("\n\n" + "="*70)
    print("PIPELINE OUTPUT COMPARISON")
    print("="*70)
    
    outputs = [
        ('local_pipeline_output', 'General (No Filters)'),
        ('local_pipeline_teacher_output', 'Teacher Audience'),
    ]
    
    for output_dir, description in outputs:
        manifest_path = Path(output_dir) / 'manifest.json'
        
        if manifest_path.exists():
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            
            print(f"\n\n{description} ({output_dir}):")
            print(f"  Files Discovered: {manifest['files_discovered']}")
            print(f"  Files Succeeded:  {manifest['files_succeeded']}")
            print(f"  Valid Chunks:     {manifest['chunks_valid']}")
            print(f"  Embeddings:       {manifest['embeddings_generated']}")
            print(f"  Provider:         {manifest['embedding_provider']}")
            
            # Show file results
            print(f"  File Processing:")
            for file_result in manifest['file_results']:
                fname = file_result['file_path'].split('/')[-1]
                print(f"    • {fname}")
                print(f"      Chunks: {file_result['chunk_count']} total, {file_result['valid_chunk_count']} valid")


if __name__ == '__main__':
    compare_outputs()
    test_retrieval()
    print("\n\n✅ Retrieval and filtering test complete!\n")
