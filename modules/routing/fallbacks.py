"""
Fallback Bundles: Embedded responses when Graeae unavailable

Provides sensible defaults for common task types.
Used when Graeae service is offline or unreachable.
"""

from typing import Dict, Optional

FALLBACK_BUNDLES = {
    'code_generation': {
        'response': '''For code generation, I recommend:

1. **Start with specifications**: Clearly define inputs, outputs, and edge cases
2. **Choose appropriate language**: Match the task requirements and ecosystem
3. **Follow established patterns**: Use idiomatic code for the language
4. **Add error handling**: Handle expected exceptions gracefully
5. **Write tests first**: TDD approach improves reliability

For complex algorithms, consider:
- Time/space complexity analysis
- Algorithmic optimizations
- Standard library vs custom implementations
- Performance profiling

Would you like me to help with a specific code task?''',
        'muses': ['grok-4-code', 'gpt-5.2', 'together-llama'],
        'cost': 0.04,
    },

    'architecture_design': {
        'response': '''For system architecture, I recommend:

1. **Understand requirements**: Clarify functional and non-functional requirements
2. **Define components**: Break system into logical, independent modules
3. **Design interfaces**: Clear contracts between components
4. **Plan scalability**: Design for growth in users, data, and features
5. **Consider resilience**: Build in fault tolerance and recovery

Key patterns to consider:
- Microservices vs monolithic tradeoffs
- Event-driven vs request-response patterns
- Caching strategies
- Database sharding and replication
- Load balancing approaches

Would you like to discuss a specific architecture challenge?''',
        'muses': ['gpt-5.2', 'gemini-3-pro', 'groq'],
        'cost': 0.04,
    },

    'api_design': {
        'response': '''For API design, I recommend:

1. **Choose paradigm**: REST, GraphQL, or gRPC based on use case
2. **Design for usability**: Consistent, intuitive endpoints
3. **Version strategically**: Plan API evolution from the start
4. **Security first**: Authentication, authorization, rate limiting
5. **Document thoroughly**: Clear examples and error documentation

REST best practices:
- Use HTTP verbs correctly (GET, POST, PUT, DELETE)
- Design resource-oriented endpoints
- Use appropriate status codes
- Implement pagination for large datasets
- Version via URL path (/v1/, /v2/)

GraphQL considerations:
- Flexible querying reduces over/under-fetching
- More complex caching requirements
- Steeper learning curve

Would you like help designing a specific API?''',
        'muses': ['gpt-5.2', 'gemini-3-pro', 'groq'],
        'cost': 0.04,
    },

    'data_modeling': {
        'response': '''For data modeling, I recommend:

1. **Understand data flow**: How data moves through the system
2. **Identify entities**: People, objects, concepts that need storage
3. **Define relationships**: How entities relate to each other
4. **Normalize as needed**: Balance normalization with query performance
5. **Plan scaling**: How will the model handle growth?

Database selection:
- **Relational (SQL)**: Structured data with clear schemas
- **Document (NoSQL)**: Flexible schemas, hierarchical data
- **Graph**: Relationship-heavy data
- **Time-series**: Time-stamped measurements

Schema design principles:
- Use surrogate keys for relationships
- Separate concerns into different tables/collections
- Plan indexes for common queries
- Consider denormalization for performance

Would you like to discuss a specific data model?''',
        'muses': ['gemini-3-pro', 'gpt-5.2', 'groq'],
        'cost': 0.04,
    },

    'reasoning': {
        'response': '''For complex reasoning:

1. **Break it down**: Decompose into smaller, manageable pieces
2. **State assumptions**: Be explicit about what you're assuming
3. **Consider alternatives**: Multiple approaches often exist
4. **Test with examples**: Validate reasoning with concrete cases
5. **Verify logic**: Check for contradictions and edge cases

Problem-solving framework:
- **Define the problem**: What are we trying to solve?
- **Gather information**: What do we know?
- **Generate options**: What approaches could work?
- **Evaluate options**: Pros/cons of each approach
- **Choose and act**: Decision and implementation

For technical problems:
- Reproduce the issue consistently
- Isolate the problem area
- Develop a hypothesis
- Test the hypothesis
- Implement the solution

What problem would you like help reasoning through?''',
        'muses': ['gpt-5.2', 'gemini-3-pro', 'groq'],
        'cost': 0.04,
    },

    'debugging': {
        'response': '''For debugging:

1. **Reproduce consistently**: Identify exact steps to reproduce
2. **Isolate the problem**: Narrow down the source
3. **Check logs**: Review error messages and stack traces
4. **Use debugger**: Step through code execution
5. **Test hypothesis**: Verify your understanding

Debugging techniques:
- Add logging at strategic points
- Use binary search (comment out half the code)
- Check variable values at key points
- Review recent changes (git log)
- Test in isolation

Common issues:
- **Off-by-one errors**: Loop boundary issues
- **Null references**: Missing null checks
- **Type mismatches**: Data type confusion
- **State corruption**: Unexpected state changes
- **Concurrency issues**: Race conditions, deadlocks

Provide error message or symptoms and I can help diagnose.''',
        'muses': ['grok-4', 'gpt-5.2', 'together-llama'],
        'cost': 0.03,
    },

    'refactoring': {
        'response': '''For refactoring:

1. **Add tests first**: Ensure behavior doesn't change
2. **Identify patterns**: Look for duplication and complexity
3. **Small changes**: Refactor in tiny, testable steps
4. **Keep tests passing**: Verify after each change
5. **Commit frequently**: Save working state often

Common refactorings:
- Extract method: Create function from code block
- Rename: Make names more meaningful
- Move: Relocate code to better module
- Replace conditional: Use pattern matching or inheritance
- Consolidate: Combine similar methods

When to refactor:
- Duplication (DRY principle)
- Long methods (extract smaller functions)
- Long parameter lists
- Unclear variable names
- Complex conditionals

What code would you like to refactor?''',
        'muses': ['grok-4-code', 'gpt-5.2', 'together-llama'],
        'cost': 0.03,
    },

    'research': {
        'response': '''For research and fact-finding:

1. **Define search terms**: What specifically are you researching?
2. **Evaluate sources**: Check credibility and recency
3. **Cross-reference**: Verify facts across multiple sources
4. **Cite sources**: Track where information comes from
5. **Synthesize**: Draw conclusions from gathered information

Research framework:
- Background: What's the current state?
- Question: What do you want to know?
- Investigation: Search and gather information
- Analysis: What does the data show?
- Conclusion: What did you learn?

For technical research:
- Check official documentation first
- Look for peer-reviewed papers
- Find case studies or examples
- Consider implementation experience
- Note version/date (tech changes fast)

What would you like to research?''',
        'muses': ['perplexity-sonar', 'gpt-5.2', 'gemini-3-pro'],
        'cost': 0.05,
    },

    'default': {
        'response': '''I'm here to help with your task. To provide the best assistance:

1. **Be specific**: Describe what you're trying to accomplish
2. **Provide context**: Share relevant background information
3. **Show examples**: Concrete examples help clarify the goal
4. **State constraints**: What limitations or requirements exist?
5. **Ask clearly**: Phrase your question as specifically as possible

Common task types I can help with:
- Code generation and debugging
- System architecture design
- API and data modeling
- Complex reasoning and analysis
- Research and synthesis
- Refactoring and optimization

What would you like help with?''',
        'muses': ['gpt-5.2', 'gemini-3-pro', 'groq'],
        'cost': 0.03,
    },
}


def get_fallback(task_type: str) -> Optional[Dict]:
    """Get fallback response for task type

    Args:
        task_type: Type of task

    Returns:
        Fallback bundle dict or None
    """
    return FALLBACK_BUNDLES.get(task_type) or FALLBACK_BUNDLES.get('default')


def list_fallbacks() -> Dict[str, Dict]:
    """List all fallback bundles

    Returns:
        Dict of all fallback bundles
    """
    return {k: v for k, v in FALLBACK_BUNDLES.items() if k != 'default'}
