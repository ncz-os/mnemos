#!/bin/bash
# Daily sync of git repos → MNEMOS
# Stores recent commits as project activity memories

MNEMOS_API="http://localhost:5002"
# Configure your git repos here. Format: 'path/to/repo.git|ProjectName|topic-tag'
REPOS=(
  # '/mnt/nas/repos/my-project.git|MyProject|my-project'
  # '/mnt/nas/repos/another-project.git|AnotherProject|another-project'
)

LOG_FILE="$HOME/.mnemos/git_sync.log"
mkdir -p "$HOME/.mnemos"

log_message() {
    local msg="$1"
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $msg" | tee -a "$LOG_FILE"
}

store_commit_in_mnemos() {
    local project="$1"
    local hash="$2"
    local author="$3"
    local date="$4"
    local message="$5"
    local tags="$6"
    
    # Create memory content
    local content="$project: $message

Author: $author
Commit: $hash
Date: $date"
    
    # POST to MNEMOS API
    local response=$(curl -s -X POST "$MNEMOS_API/memories" \
        -H "Content-Type: application/json" \
        -d @- << JSONEOF
{
    "content": $(printf '%s\n' "$content" | jq -Rs .),
    "category": "project_activity",
    "tags": ["git", "commit", "$project"],
    "metadata": {
        "project": "$project",
        "commit_hash": "$hash",
        "author": "$author",
        "commit_date": "$date",
        "sync_timestamp": "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    }
}
JSONEOF
    )
    
    # Check if successful
    if echo "$response" | grep -q '"id"'; then
        local mem_id=$(echo "$response" | grep -o '"id":"[^"]*' | cut -d'"' -f4)
        echo "    ✓ Stored: $hash ($message) -> $mem_id"
        return 0
    else
        echo "    ✗ Failed to store: $hash"
        return 1
    fi
}

log_message "Starting git sync to MNEMOS"

for repo_config in "${REPOS[@]}"; do
    repo_path=$(echo "$repo_config" | cut -d'|' -f1)
    project=$(echo "$repo_config" | cut -d'|' -f2)
    project_tag=$(echo "$repo_config" | cut -d'|' -f3)
    
    if [ ! -d "$repo_path" ]; then
        log_message "  [SKIP] $project - repo not found at $repo_path"
        continue
    fi
    
    log_message "  Processing $project..."
    
    # Get recent commits with full details
    # Using format: hash|author|date|subject
    commit_count=0
    stored_count=0
    
    cd "$repo_path"
    while IFS='|' read -r hash author date subject; do
        [ -z "$hash" ] && continue
        ((commit_count++))
        
        if store_commit_in_mnemos "$project" "$hash" "$author" "$date" "$subject" "$project_tag"; then
            ((stored_count++))
        fi
    done < <(git log --format="%h|%an|%ai|%s" -30 2>/dev/null)
    
    log_message "  ✓ $project: Processed $commit_count commits, stored $stored_count"
done

log_message "Git sync complete"
