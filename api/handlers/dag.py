"""DAG (Directed Acyclic Graph) endpoints for memory versioning.

Implements git-like operations on memory history:
- log: Walk commit DAG from HEAD to root
- branches: List all branches for a memory
- branch: Create new branch from HEAD or specific commit
- checkout: Fetch commit content by hash
- merge: Merge source_branch into target_branch
"""

import logging
import time as _time
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/memories", tags=["dag"])


# ────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ────────────────────────────────────────────────────────────────────────────

class CommitInfo(BaseModel):
    commit_hash: str
    version_num: int
    parent_hash: Optional[str] = None
    branch: str
    content: str
    category: str
    subcategory: Optional[str] = None
    snapshot_at: str
    snapshot_by: Optional[str] = None
    change_type: str  # create, update, delete


class BranchInfo(BaseModel):
    name: str
    head_commit_hash: str
    created_at: str
    created_by: Optional[str] = None


class BranchCreateRequest(BaseModel):
    name: str
    from_commit: Optional[str] = None  # commit hash; default = HEAD


class MergeRequest(BaseModel):
    source_branch: str
    strategy: str = "latest-wins"  # latest-wins or manual


class MergeResult(BaseModel):
    success: bool
    new_commit_hash: Optional[str] = None
    message: str


def _require_pool():
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    return _lc._pool


# ────────────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────────────

@router.get("/{memory_id}/log", response_model=List[CommitInfo])
async def get_memory_log(
    memory_id: str,
    branch: str = Query("main", description="Branch to walk from HEAD"),
    limit: int = Query(50, le=500),
    user: UserContext = Depends(get_current_user),
):
    """Walk commit DAG from branch HEAD to root.

    Returns commit history (commits reachable from HEAD via parent pointers).
    Equivalent to `git log`.
    """
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            # Recursive CTE: walk from HEAD backward through parent_version_id
            rows = await conn.fetch(
                """
                WITH RECURSIVE commit_walk AS (
                    -- Base: START from branch HEAD
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type, 1 AS depth
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON (
                        mb.memory_id = mv.memory_id AND
                        mb.name = $2 AND
                        mb.head_version_id = mv.id
                    )
                    WHERE mv.memory_id = $1
                    UNION ALL
                    -- Recursive: WALK backward via parent_version_id
                    SELECT
                        mv.id, mv.memory_id, mv.commit_hash, mv.parent_version_id,
                        mv.version_num, mv.branch, mv.content, mv.category,
                        mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                        cw.depth + 1
                    FROM memory_versions mv
                    INNER JOIN commit_walk cw ON mv.id = cw.parent_version_id
                    WHERE cw.depth < $4
                )
                SELECT
                    commit_hash, version_num, branch, content, category, subcategory,
                    snapshot_at, snapshot_by, change_type
                FROM commit_walk
                ORDER BY depth ASC
                LIMIT $4
                """,
                memory_id,
                branch,
                memory_id,  # re-check owner in WHERE? Optional: add owner_id filter
                limit,
            )

            if not rows:
                raise HTTPException(status_code=404, detail=f"Branch '{branch}' not found")

            # Assemble with parent hashes
            commits = []
            for i, row in enumerate(rows):
                parent_hash = rows[i + 1]["commit_hash"] if i + 1 < len(rows) else None
                commits.append(
                    CommitInfo(
                        commit_hash=row["commit_hash"],
                        version_num=row["version_num"],
                        parent_hash=parent_hash,
                        branch=row["branch"],
                        content=row["content"],
                        category=row["category"],
                        subcategory=row["subcategory"],
                        snapshot_at=row["snapshot_at"].isoformat(),
                        snapshot_by=row["snapshot_by"],
                        change_type=row["change_type"],
                    )
                )

            logger.info(f"[DAG] Log: {memory_id}/{branch} returned {len(commits)} commits")
            return commits

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Log failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{memory_id}/branches", response_model=List[BranchInfo])
async def get_memory_branches(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """List all branches for a memory."""
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            branches = await conn.fetch(
                """
                SELECT
                    mb.name, mv.commit_hash, mb.created_at, mb.created_by
                FROM memory_branches mb
                LEFT JOIN memory_versions mv ON mv.id = mb.head_version_id
                WHERE mb.memory_id = $1
                ORDER BY mb.created_at DESC
                """,
                memory_id,
            )

            return [
                BranchInfo(
                    name=b["name"],
                    head_commit_hash=b["commit_hash"],
                    created_at=b["created_at"].isoformat(),
                    created_by=b["created_by"],
                )
                for b in branches
            ]

    except Exception as e:
        logger.error(f"[DAG] Branches failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{memory_id}/branch", response_model=BranchInfo)
async def create_branch(
    memory_id: str,
    request: BranchCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create new branch from HEAD or specific commit hash."""
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            # Resolve starting point (HEAD or specific commit)
            if request.from_commit:
                start_version = await conn.fetchrow(
                    "SELECT id, commit_hash, created_at FROM memory_versions WHERE memory_id = $1 AND commit_hash = $2",
                    memory_id,
                    request.from_commit,
                )
                if not start_version:
                    raise HTTPException(status_code=404, detail="Commit hash not found")
            else:
                # Default: use current main branch HEAD
                start_version = await conn.fetchrow(
                    """
                    SELECT mv.id, mv.commit_hash, mv.created_at
                    FROM memory_versions mv
                    INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                    WHERE mv.memory_id = $1 AND mb.name = 'main'
                    """,
                    memory_id,
                )
                if not start_version:
                    raise HTTPException(status_code=404, detail="main branch HEAD not found")

            # Create branch record
            await conn.fetchval(
                """
                INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (memory_id, name) DO UPDATE
                SET head_version_id = EXCLUDED.head_version_id
                RETURNING id
                """,
                memory_id,
                request.name,
                start_version["id"],
                user.user_id,
            )

            logger.info(f"[DAG] Branch '{request.name}' created for {memory_id}")

            return BranchInfo(
                name=request.name,
                head_commit_hash=start_version["commit_hash"],
                created_at=start_version["created_at"].isoformat(),
                created_by=user.user_id,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Branch creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{memory_id}/commits/{commit_hash}", response_model=CommitInfo)
async def get_commit(
    memory_id: str,
    commit_hash: str,
    user: UserContext = Depends(get_current_user),
):
    """Fetch commit content by hash."""
    pool = _require_pool()

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    mv.commit_hash, mv.version_num, mv.branch, mv.content, mv.category,
                    mv.subcategory, mv.snapshot_at, mv.snapshot_by, mv.change_type,
                    (SELECT commit_hash FROM memory_versions mv2
                     WHERE mv2.id = mv.parent_version_id) AS parent_hash
                FROM memory_versions mv
                WHERE mv.memory_id = $1 AND mv.commit_hash = $2
                """,
                memory_id,
                commit_hash,
            )

            if not row:
                raise HTTPException(status_code=404, detail="Commit not found")

            return CommitInfo(
                commit_hash=row["commit_hash"],
                version_num=row["version_num"],
                parent_hash=row["parent_hash"],
                branch=row["branch"],
                content=row["content"],
                category=row["category"],
                subcategory=row["subcategory"],
                snapshot_at=row["snapshot_at"].isoformat(),
                snapshot_by=row["snapshot_by"],
                change_type=row["change_type"],
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Commit fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{memory_id}/merge", response_model=MergeResult)
async def merge_branch(
    memory_id: str,
    request: MergeRequest,
    target_branch: str = Query("main"),
    user: UserContext = Depends(get_current_user),
):
    """Merge source_branch into target_branch.

    Strategy 'latest-wins' takes source_branch HEAD content.
    Strategy 'manual' requires manual conflict resolution (not implemented yet).
    """
    pool = _require_pool()

    if request.strategy not in ("latest-wins", "manual"):
        raise HTTPException(status_code=400, detail="Invalid merge strategy")

    try:
        async with pool.acquire() as conn:
            # Get source and target branch HEADs
            source_head = await conn.fetchrow(
                """
                SELECT mv.id, mv.commit_hash, mv.content, mv.version_num
                FROM memory_versions mv
                INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                WHERE mv.memory_id = $1 AND mb.name = $2
                """,
                memory_id,
                request.source_branch,
            )

            target_head = await conn.fetchrow(
                """
                SELECT mv.id, mv.version_num
                FROM memory_versions mv
                INNER JOIN memory_branches mb ON mb.head_version_id = mv.id
                WHERE mv.memory_id = $1 AND mb.name = $2
                """,
                memory_id,
                target_branch,
            )

            if not source_head:
                raise HTTPException(status_code=404, detail=f"Source branch '{request.source_branch}' not found")
            if not target_head:
                raise HTTPException(status_code=404, detail=f"Target branch '{target_branch}' not found")

            # Apply merge strategy
            if request.strategy == "latest-wins":
                # Create merge commit on target_branch with source content
                next_version = target_head["version_num"] + 1
                # Compute merge hash from both parent commits (ensures uniqueness and tamper-evidence)
                import hashlib
                merge_hash = hashlib.sha256(
                    f"{source_head['commit_hash']}{target_head['commit_hash']}{int(_time.time() * 1000)}".encode()
                ).hexdigest()[:16]

                new_commit_id = await conn.fetchval(
                    """
                    INSERT INTO memory_versions (
                        memory_id, version_num, content, category, subcategory,
                        branch, commit_hash, parent_version_id, snapshot_by, change_type
                    )
                    SELECT
                        $1, $2, $3, category, subcategory,
                        $4, $5, $6, $7, 'merge'
                    FROM memory_versions WHERE id = $8
                    RETURNING id
                    """,
                    memory_id,
                    next_version,
                    source_head["content"],
                    target_branch,
                    merge_hash,  # Use computed merge hash, not source hash
                    target_head["id"],
                    user.user_id,
                    source_head["id"],
                )

                # Update target branch HEAD
                await conn.execute(
                    "UPDATE memory_branches SET head_version_id = $1 WHERE memory_id = $2 AND name = $3",
                    new_commit_id,
                    memory_id,
                    target_branch,
                )

                logger.info(f"[DAG] Merged {request.source_branch} → {target_branch} for {memory_id} (merge_hash={merge_hash})")

                return MergeResult(
                    success=True,
                    new_commit_hash=merge_hash,  # Return the new merge commit hash
                    message=f"Merged {request.source_branch} into {target_branch}",
                )

            else:  # manual
                return MergeResult(
                    success=False,
                    message="Manual merge strategy not yet implemented",
                )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DAG] Merge failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
