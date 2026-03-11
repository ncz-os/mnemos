#!/bin/bash
# Monitor MNEMOS distillation progress in real-time

INTERVAL=${1:-10}  # refresh interval (seconds)
PG_HOST=localhost
PG_PORT=5432
PG_DATABASE=mnemos
PG_USER=mnemos_user
PG_PASSWORD=mnemos_secure_password

export PGPASSWORD=$PG_PASSWORD

get_stats() {
    psql -h $PG_HOST -U $PG_USER -d $PG_DATABASE -c """
    SELECT 
        COUNT(*) as total_memories,
        COUNT(CASE WHEN llm_optimized = true THEN 1 END) as optimized,
        COUNT(CASE WHEN llm_optimized = false THEN 1 END) as pending,
        COUNT(CASE WHEN (metadata->>'distillation_attempts')::int >= 3 THEN 1 END) as max_attempts_hit,
        AVG(quality_rating)::numeric(5,1) as avg_quality,
        SUM(CASE WHEN compressed_content IS NOT NULL THEN LENGTH(content) - LENGTH(compressed_content) ELSE 0 END) as bytes_saved
    FROM memories;
    """ 2>/dev/null | tail -2
}

get_rate() {
    psql -h $PG_HOST -U $PG_USER -d $PG_DATABASE -c """
    SELECT 
        COUNT(CASE WHEN optimized_at > NOW() - INTERVAL '1 minute' THEN 1 END) as per_minute,
        COUNT(CASE WHEN optimized_at > NOW() - INTERVAL '1 hour' THEN 1 END) as per_hour
    FROM memories
    WHERE llm_optimized = true;
    """ 2>/dev/null | tail -2
}

get_worker_status() {
    sudo journalctl -u mnemos-distillation.service -n 1 --no-pager 2>/dev/null | grep -E "✅|⏱|❌|Processing|Progress" | tail -1
}

clear
while true; do
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║        MNEMOS DISTILLATION PROGRESS MONITOR                     ║"
    echo "║        $(date '+%Y-%m-%d %H:%M:%S')                              ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "📊 DATABASE STATS:"
    get_stats | awk '{if(NR==2) printf "   Total Memories: %s | Optimized: %s | Pending: %s | Max Attempts Hit: %s\n   Avg Quality: %s | Bytes Saved: %s\n", $1, $2, $3, $4, $5, $6}'
    echo ""
    echo "📈 PROCESSING RATE:"
    get_rate | awk '{if(NR==2) printf "   Per Minute: %s memories | Per Hour: %s memories\n", $1, $2}'
    echo ""
    echo "🔧 WORKER STATUS:"
    echo "   $(get_worker_status)"
    echo ""
    echo "⏱  Refreshing in ${INTERVAL}s (Ctrl+C to exit)..."
    sleep $INTERVAL
    clear
done
