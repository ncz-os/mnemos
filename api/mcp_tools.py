"""MCP (Memory Context Protocol) tools for MNEMOS.

Exposes key MNEMOS functionality as tools accessible from OpenClaw and other agents:
- log_memory: Walk DAG commit history
- branch_memory: Create named branches
- diff_memory_commits: Unified diff between commits
- checkout_memory: Fetch commit content by hash
- recommend_model: Query cost optimizer
"""

import logging
from typing import Dict, Any, Optional

import api.lifecycle as _lc
from api.auth import UserContext

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Tool Implementations
# ────────────────────────────────────────────────────────────────────────────

async def tool_log_memory(
    memory_id: str,
    branch: str = "main",
    limit: int = 50,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Walk commit DAG from branch HEAD to root.

    Returns list of commits with hashes, change types, and metadata.
    Equivalent to `git log`.

    Args:
        memory_id: Memory ID to walk
        branch: Branch name (default: main)
        limit: Max commits to return (default: 50)
        user: User context for auth (optional)

    Returns:
        Dict with commits list and metadata
    """
    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE commit_walk AS (
                    SELECT
                        mv.id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.change_type, mv.snapshot_at, mv.snapshot_by, 1 AS depth
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON (
                        mb.memory_id = mv.memory_id AND
                        mb.name = $2 AND
                        mb.head_version_id = mv.id
                    )
                    WHERE mv.memory_id = $1
                    UNION ALL
                    SELECT
                        mv.id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.change_type, mv.snapshot_at, mv.snapshot_by, cw.depth + 1
                    FROM memory_versions mv
                    INNER JOIN commit_walk cw ON mv.id = cw.parent_version_id
                    WHERE cw.depth < $4
                )
                SELECT
                    commit_hash, version_num, branch, category, change_type,
                    snapshot_at, snapshot_by
                FROM commit_walk
                ORDER BY depth ASC
                LIMIT $4
                """,
                memory_id,
                branch,
                memory_id,
                limit,
            )

            return {
                "success": True,
                "memory_id": memory_id,
                "branch": branch,
                "commits": [
                    {
                        "hash": r["commit_hash"],
                        "version": r["version_num"],
                        "type": r["change_type"],
                        "category": r["category"],
                        "timestamp": r["snapshot_at"].isoformat(),
                        "author": r["snapshot_by"],
                    }
                    for r in rows
                ],
                "count": len(rows),
            }

    except Exception as e:
        logger.error(f"[MCP] log_memory failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_branch_memory(
    memory_id: str,
    name: str,
    from_commit: Optional[str] = None,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Create new branch from HEAD or specific commit.

    Args:
        memory_id: Memory ID to branch
        name: New branch name
        from_commit: Commit hash to branch from (default: main HEAD)
        user: User context for auth

    Returns:
        Dict with branch creation status and details
    """
    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            # Resolve starting point
            if from_commit:
                start = await conn.fetchrow(
                    "SELECT id, commit_hash FROM memory_versions WHERE memory_id = $1 AND commit_hash = $2",
                    memory_id,
                    from_commit,
                )
                if not start:
                    return {"success": False, "error": "Commit not found"}
            else:
                start = await conn.fetchrow(
                    """
                    SELECT mv.id, mv.commit_hash
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = 'main'
                    """,
                    memory_id,
                )
                if not start:
                    return {"success": False, "error": "main branch not found"}

            # Create branch
            await conn.execute(
                """
                INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (memory_id, name) DO UPDATE
                SET head_version_id = EXCLUDED.head_version_id
                """,
                memory_id,
                name,
                start["id"],
                user.user_id if user else None,
            )

            return {
                "success": True,
                "memory_id": memory_id,
                "branch": name,
                "commit_hash": start["commit_hash"],
                "created_by": user.user_id if user else None,
            }

    except Exception as e:
        logger.error(f"[MCP] branch_memory failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_diff_memory_commits(
    memory_id: str,
    commit_a: str,
    commit_b: str,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Generate unified diff between two commits.

    Args:
        memory_id: Memory ID
        commit_a: First commit hash (older)
        commit_b: Second commit hash (newer)
        user: User context for auth

    Returns:
        Dict with unified diff and metadata
    """
    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            # Fetch both commits
            commit_a_row = await conn.fetchrow(
                "SELECT content, version_num FROM memory_versions WHERE memory_id = $1 AND commit_hash = $2",
                memory_id,
                commit_a,
            )
            commit_b_row = await conn.fetchrow(
                "SELECT content, version_num FROM memory_versions WHERE memory_id = $1 AND commit_hash = $2",
                memory_id,
                commit_b,
            )

            if not commit_a_row or not commit_b_row:
                return {"success": False, "error": "One or both commits not found"}

            # Generate simple unified diff
            import difflib
            diff = difflib.unified_diff(
                commit_a_row["content"].splitlines(keepends=True),
                commit_b_row["content"].splitlines(keepends=True),
                fromfile=f"{commit_a[:8]} (v{commit_a_row['version_num']})",
                tofile=f"{commit_b[:8]} (v{commit_b_row['version_num']})",
                lineterm="",
            )

            return {
                "success": True,
                "memory_id": memory_id,
                "from_commit": commit_a,
                "to_commit": commit_b,
                "diff": "".join(diff),
            }

    except Exception as e:
        logger.error(f"[MCP] diff_memory_commits failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_checkout_memory(
    memory_id: str,
    commit_hash: str,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Fetch commit content and metadata by hash.

    Args:
        memory_id: Memory ID
        commit_hash: Commit hash to fetch
        user: User context for auth

    Returns:
        Dict with commit content and metadata
    """
    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    commit_hash, version_num, branch, category, subcategory,
                    content, change_type, snapshot_at, snapshot_by
                FROM memory_versions
                WHERE memory_id = $1 AND commit_hash = $2
                """,
                memory_id,
                commit_hash,
            )

            if not row:
                return {"success": False, "error": "Commit not found"}

            return {
                "success": True,
                "memory_id": memory_id,
                "commit": {
                    "hash": row["commit_hash"],
                    "version": row["version_num"],
                    "branch": row["branch"],
                    "type": row["change_type"],
                    "category": row["category"],
                    "subcategory": row["subcategory"],
                    "timestamp": row["snapshot_at"].isoformat(),
                    "author": row["snapshot_by"],
                },
                "content": row["content"],
            }

    except Exception as e:
        logger.error(f"[MCP] checkout_memory failed: {e}")
        return {"success": False, "error": str(e)}


async def tool_recommend_model(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Query model optimizer for cost-aware recommendation.

    Args:
        task_type: Task type (code_generation, reasoning, architecture_design, etc.)
        cost_budget: Max cost per 1M tokens (default: $10)
        quality_floor: Min quality score (default: 0.85)
        user: User context for auth

    Returns:
        Dict with recommended model and reasoning
    """
    pool = _lc._pool
    if not pool:
        return {"success": False, "error": "Database unavailable"}

    try:
        # Map task types to capabilities
        capability_map = {
            "code_generation": ["coding"],
            "reasoning": ["reasoning", "logic"],
            "architecture_design": ["reasoning"],
            "summarization": ["reasoning"],
            "web_search": ["online", "search"],
        }
        required_caps = capability_map.get(task_type, ["reasoning"])

        async with pool.acquire() as conn:
            # Find models meeting criteria
            models = await conn.fetch(
                """
                SELECT
                    provider, model_id, display_name,
                    input_cost_per_mtok, output_cost_per_mtok,
                    graeae_weight, context_window
                FROM model_registry
                WHERE available = true
                AND deprecated = false
                AND graeae_weight >= $1
                AND (input_cost_per_mtok + output_cost_per_mtok) / 2.0 <= $2
                AND capabilities @> $3
                ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                LIMIT 1
                """,
                quality_floor,
                cost_budget,
                required_caps,
            )

            if not models:
                # Fallback
                models = await conn.fetch(
                    """
                    SELECT
                        provider, model_id, display_name,
                        input_cost_per_mtok, output_cost_per_mtok,
                        graeae_weight, context_window
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                    LIMIT 1
                    """
                )

            if not models:
                return {"success": False, "error": "No models available"}

            model = models[0]
            avg_cost = (model["input_cost_per_mtok"] + model["output_cost_per_mtok"]) / 2.0

            return {
                "success": True,
                "task_type": task_type,
                "recommended": {
                    "provider": model["provider"],
                    "model_id": model["model_id"],
                    "display_name": model.get("display_name"),
                    "cost_per_mtok": float(avg_cost),
                    "quality_score": float(model["graeae_weight"]),
                    "context_window": model.get("context_window"),
                },
                "reasoning": f"Cheapest model with {', '.join(required_caps)} capability above quality floor {quality_floor}",
                "budget_met": avg_cost <= cost_budget,
            }

    except Exception as e:
        logger.error(f"[MCP] recommend_model failed: {e}")
        return {"success": False, "error": str(e)}


# ────────────────────────────────────────────────────────────────────────────
# Tool Registry
# ────────────────────────────────────────────────────────────────────────────

TOOLS = {
    "log_memory": {
        "description": "Walk commit DAG from branch HEAD to root",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "branch": {"type": "string", "description": "Branch name (default: main)"},
            "limit": {"type": "integer", "description": "Max commits (default: 50)"},
        },
        "required": ["memory_id"],
        "handler": tool_log_memory,
    },
    "branch_memory": {
        "description": "Create new branch from HEAD or specific commit",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "name": {"type": "string", "description": "New branch name"},
            "from_commit": {"type": "string", "description": "Commit hash (default: main HEAD)"},
        },
        "required": ["memory_id", "name"],
        "handler": tool_branch_memory,
    },
    "diff_memory_commits": {
        "description": "Generate unified diff between two commits",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "commit_a": {"type": "string", "description": "First commit hash (older)"},
            "commit_b": {"type": "string", "description": "Second commit hash (newer)"},
        },
        "required": ["memory_id", "commit_a", "commit_b"],
        "handler": tool_diff_memory_commits,
    },
    "checkout_memory": {
        "description": "Fetch commit content and metadata by hash",
        "parameters": {
            "memory_id": {"type": "string", "description": "Memory ID"},
            "commit_hash": {"type": "string", "description": "Commit hash to fetch"},
        },
        "required": ["memory_id", "commit_hash"],
        "handler": tool_checkout_memory,
    },
    "recommend_model": {
        "description": "Query model optimizer for cost-aware recommendation",
        "parameters": {
            "task_type": {
                "type": "string",
                "description": "Task type (code_generation, reasoning, architecture_design, etc.)",
            },
            "cost_budget": {"type": "number", "description": "Max $/MTok (default: 10.0)"},
            "quality_floor": {"type": "number", "description": "Min quality score (default: 0.85)"},
        },
        "required": ["task_type"],
        "handler": tool_recommend_model,
    },
}


async def execute_tool(
    tool_name: str,
    parameters: Dict[str, Any],
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Execute an MCP tool.

    Args:
        tool_name: Name of tool to execute
        parameters: Tool parameters
        user: User context for auth

    Returns:
        Tool result dict
    """
    if tool_name not in TOOLS:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    tool_info = TOOLS[tool_name]
    handler = tool_info["handler"]

    # Add user context to parameters
    parameters["user"] = user

    try:
        result = await handler(**parameters)
        logger.info(f"[MCP] Tool {tool_name} executed successfully")
        return result
    except Exception as e:
        logger.error(f"[MCP] Tool {tool_name} failed: {e}")
        return {"success": False, "error": str(e)}
