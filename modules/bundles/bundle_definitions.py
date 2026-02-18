"""
BundleDefinition: Consultation bundle configurations

Bundles group task types with specialized model variants.
Each bundle has:
- Primary, secondary, tertiary models
- Fallback response
- Consensus score requirement
- Tags for filtering
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class BundleDefinition:
    """Represents a consultation bundle"""

    bundle_type: str
    description: str
    models: Dict[str, str]  # provider → model mapping
    consensus_score: int    # Minimum consensus score (0-100)
    fallback_response: Optional[str]
    tags: List[str]
    compression_ratio: float = 0.4

    def to_dict(self) -> Dict:
        return {
            'bundle_type': self.bundle_type,
            'description': self.description,
            'models': self.models,
            'consensus_score': self.consensus_score,
            'fallback_response': self.fallback_response,
            'tags': self.tags,
            'compression_ratio': self.compression_ratio,
        }


# Code Generation Bundle
CODE_GENERATION = BundleDefinition(
    bundle_type='code_generation',
    description='Specialized bundle for code generation tasks',
    models={
        'xai_grok': 'grok-4-code',           # Grok fast-coding variant
        'openai': 'gpt-5.2',                 # OpenAI frontier
        'together_ai': 'meta/llama-3-70b',   # Together open source
    },
    consensus_score=85,
    fallback_response='Unable to generate code at this time',
    tags=['coding', 'algorithms', 'implementation'],
    compression_ratio=0.35,
)

# Architecture Design Bundle
ARCHITECTURE_DESIGN = BundleDefinition(
    bundle_type='architecture_design',
    description='Multi-model consensus for system architecture',
    models={
        'openai': 'gpt-5.2',                 # OpenAI reasoning
        'google': 'gemini-3-pro',            # Google multimodal
        'groq': 'llama-3.3-70b',             # Groq speed
    },
    consensus_score=87,
    fallback_response='Unable to provide architecture recommendation at this time',
    tags=['system_design', 'microservices', 'infrastructure'],
    compression_ratio=0.40,
)

# API Design Bundle
API_DESIGN = BundleDefinition(
    bundle_type='api_design',
    description='API and schema design consensus',
    models={
        'openai': 'gpt-5.2',                 # OpenAI reasoning
        'google': 'gemini-3-pro',            # Google multimodal
        'groq': 'llama-3.3-70b',             # Groq speed
    },
    consensus_score=85,
    fallback_response='Unable to design API at this time',
    tags=['api', 'rest', 'graphql', 'schema'],
    compression_ratio=0.40,
)

# Data Modeling Bundle
DATA_MODELING = BundleDefinition(
    bundle_type='data_modeling',
    description='Database schema and data structure design',
    models={
        'google': 'gemini-3-pro',            # Gemini multimodal
        'openai': 'gpt-5.2',                 # OpenAI reasoning
        'groq': 'llama-3.3-70b',             # Groq speed
    },
    consensus_score=88,
    fallback_response='Unable to model data at this time',
    tags=['database', 'schema', 'data_structure', 'modeling'],
    compression_ratio=0.40,
)

# Complex Reasoning Bundle
REASONING = BundleDefinition(
    bundle_type='reasoning',
    description='Deep reasoning and problem solving',
    models={
        'openai': 'gpt-5.2',                 # OpenAI reasoning
        'google': 'gemini-3-pro',            # Google multimodal
        'groq': 'llama-3.3-70b',             # Groq speed
    },
    consensus_score=82,
    fallback_response='Unable to reason through this problem at this time',
    tags=['reasoning', 'analysis', 'problem_solving'],
    compression_ratio=0.40,
)

# Debugging Bundle
DEBUGGING = BundleDefinition(
    bundle_type='debugging',
    description='Error diagnosis and debugging',
    models={
        'xai_grok': 'grok-4',                # Grok reasoning
        'openai': 'gpt-5.2',                 # OpenAI
        'together_ai': 'meta/llama-3-70b',   # Together
    },
    consensus_score=80,
    fallback_response='Unable to debug at this time',
    tags=['debugging', 'error', 'diagnosis'],
    compression_ratio=0.35,
)

# Refactoring Bundle
REFACTORING = BundleDefinition(
    bundle_type='refactoring',
    description='Code refactoring and optimization',
    models={
        'xai_grok': 'grok-4-code',           # Grok code variant
        'openai': 'gpt-5.2',                 # OpenAI
        'together_ai': 'meta/llama-3-70b',   # Together
    },
    consensus_score=83,
    fallback_response='Unable to refactor at this time',
    tags=['refactoring', 'optimization', 'cleanup'],
    compression_ratio=0.40,
)

# Research/Analysis Bundle
RESEARCH = BundleDefinition(
    bundle_type='research',
    description='Research synthesis with web search',
    models={
        'perplexity': 'sonar-pro',           # Perplexity web search
        'openai': 'gpt-5.2',                 # OpenAI
        'google': 'gemini-3-pro',            # Google
    },
    consensus_score=85,
    fallback_response='Unable to research at this time',
    tags=['research', 'analysis', 'synthesis', 'web_search'],
    compression_ratio=0.40,
)

# Bundle registry
BUNDLES = {
    'code_generation': CODE_GENERATION,
    'architecture_design': ARCHITECTURE_DESIGN,
    'api_design': API_DESIGN,
    'data_modeling': DATA_MODELING,
    'reasoning': REASONING,
    'debugging': DEBUGGING,
    'refactoring': REFACTORING,
    'research': RESEARCH,
}

BUNDLE_NAMES = {
    'code': CODE_GENERATION,
    'architecture': ARCHITECTURE_DESIGN,
    'api': API_DESIGN,
    'data': DATA_MODELING,
    'reason': REASONING,
    'debug': DEBUGGING,
    'refactor': REFACTORING,
    'research': RESEARCH,
}


def get_bundle(bundle_type: str) -> Optional[BundleDefinition]:
    """Get bundle by type

    Args:
        bundle_type: Bundle name

    Returns:
        BundleDefinition or None
    """
    return BUNDLES.get(bundle_type) or BUNDLE_NAMES.get(bundle_type.lower())


def list_bundles() -> List[BundleDefinition]:
    """Get all bundles

    Returns:
        List of BundleDefinition objects
    """
    return list(BUNDLES.values())


def get_bundles_by_tag(tag: str) -> List[BundleDefinition]:
    """Get bundles by tag

    Args:
        tag: Tag to filter by

    Returns:
        List of matching bundles
    """
    return [b for b in BUNDLES.values() if tag in b.tags]
